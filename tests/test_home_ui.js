/* Host-only Home interaction tests. Uses actual catalog/core/app/CSS in Chromium.
   NODE_PATH=<bundled node_modules> node tests/test_home_ui.js [screenshot directory]
   TV inventory, launches and fixed Spark endpoints are explicit local fixtures. */
'use strict';
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const {chromium} = require('playwright');
const HOME = process.env.R2D2_HOME_SOURCE || path.join(__dirname, '..', 'tv', 'home');
const ORIGIN = 'http://home.test';
const SPARK = '192.0.2.20';
const KEY = 'a'.repeat(64);
const errors = [];
let groups = 0;
function source(name, keyed = true, logos = null) {
  let value = fs.readFileSync(path.join(HOME, name + '.in'), 'utf8')
    .replaceAll('{{SPARK_IP}}', SPARK);
  if (name === 'catalog.js' && keyed) value = value.replace('"key":null', '"key":"' + KEY + '"');
  // Same single slot tools/build.py fills from --home-logos-file.
  if (name === 'catalog.js' && logos) value = value.replace('"logos":{}', '"logos":' + JSON.stringify(logos));
  return value;
}
// Synthetic logos only; no brand artwork is part of the source or tests.
const svgLogo = fill => 'data:image/svg+xml;base64,' + Buffer.from('<svg xmlns="http://www.w3.org/2000/svg" width="40" height="20"><rect width="40" height="20" rx="4" fill="' + fill + '"/></svg>').toString('base64');
// Spark's Home voice view: exact keys, closed copy. Synthetic content only.
const IDLE_VOICE = {v:1,seq:1,phase:'idle',ageMs:0,tone:'idle',word:'',headline:'',detail:'',badge:null,meter:null,meterMs:null,dwellMs:null,mood:'idle',chip:'ready'};
// Spark replies with json.dumps' default ensure_ascii: curly quotes and ellipses travel as \u escapes.
const asciiJson = value => JSON.stringify(value).replace(/[\u007f-\uffff]/g,c => '\\u' + c.charCodeAt(0).toString(16).padStart(4,'0'));
function voiceServer(initial) {
  const voice = {snapshot:{...IDLE_VOICE,...initial}, waiters:[], requests:[], raw:null};
  voice.reply = route => route.fulfill({status:200,contentType:'application/json',headers:{'Access-Control-Allow-Origin':ORIGIN,'Cache-Control':'no-store'},body:voice.raw !== null ? voice.raw : asciiJson(voice.snapshot)}).catch(() => {});
  voice.release = () => voice.waiters.splice(0).forEach(wake => wake());
  return voice;
}
function pushVoice(voice, patch) { voice.snapshot = {...IDLE_VOICE, chip:voice.snapshot.chip, ...patch, seq:voice.snapshot.seq + 1}; voice.release(); }
async function fixture(browser, options = {}) {
  const context = await browser.newContext({viewport: options.viewport || {width:1920,height:1080}, reducedMotion: options.reducedMotion || 'no-preference'});
  const voice = voiceServer(options.voice);
  await context.addInitScript(({inventory,deleteMode,protectedApp,calm}) => {
    // Visibility is emulated so the feed's hidden/visible lifecycle is observable.
    let hidden = false;
    Object.defineProperty(Document.prototype,'hidden',{configurable:true,get:() => hidden});
    Object.defineProperty(Document.prototype,'visibilityState',{configurable:true,get:() => hidden ? 'hidden' : 'visible'});
    window.fixtureSetHidden = value => { hidden = value; document.dispatchEvent(new Event('visibilitychange')); };
    if (calm) localStorage.setItem('baaz.calm.v1','true');
    window.fixtureStorage = []; const setItem = Storage.prototype.setItem;
    Storage.prototype.setItem = function (name, value) { window.fixtureStorage.push(name); return setItem.call(this,name,value); };
    window.fixtureLaunches = []; window.fixtureRemovals = []; window.fixtureRequests = []; window.fixtureInstalled = null;
    window.webOSServiceBridge = function () {};
    window.webOSServiceBridge.prototype.cancel = function () { this.fixtureCancelled = true; };
    window.webOSServiceBridge.prototype.call = function (uri, raw) {
      let reply; window.fixtureRequests.push({uri,payload:JSON.parse(raw)});
      if (uri.endsWith('/listApps')) {
        const ids = inventory || window.OwnerCatalog.library.concat(window.OwnerCatalog.shortcuts.map(item => item.id));
        // Exercise the real normalizer with duplicate and unrelated voice entries.
        if (!window.fixtureInstalled) window.fixtureInstalled = ids.concat(['netflix','local.owner.r2d2mic']).map(id => ({id,title:id,type:'web',visible:true,removable:window.OwnerCatalog.library.includes(id) && id !== protectedApp,systemApp:!window.OwnerCatalog.library.includes(id) || id === protectedApp}));
        reply = {returnValue:true, apps:window.fixtureInstalled};
      } else if (uri === 'luna://com.webos.appInstallService/remove') {
        const payload=JSON.parse(raw); window.fixtureRemovals.push(payload);
        if (deleteMode === 'hold') { window.fixtureResolveRemoval = () => { window.fixtureInstalled=window.fixtureInstalled.filter(app=>app.id!==payload.id); this.onservicecallback(JSON.stringify({returnValue:true})); }; window.fixtureRejectRemoval = () => this.onservicecallback(JSON.stringify({returnValue:false})); return; }
        if (deleteMode === 'denied') reply={returnValue:false,errorText:'Fixture permission denied'};
        else if (deleteMode === 'ack-only') reply={returnValue:true};
        else { window.fixtureInstalled=window.fixtureInstalled.filter(app=>app.id!==payload.id); reply={returnValue:true}; }
      } else if (uri.endsWith('/launch')) {
        window.fixtureLaunches.push(JSON.parse(raw)); reply = {returnValue:true};
      } else throw Error('UNEXPECTED_TV_OPERATION');
      queueMicrotask(() => this.onservicecallback(JSON.stringify(reply)));
    };
  }, {inventory:options.inventory || null,deleteMode:options.deleteMode || "success",protectedApp:options.protectedApp || null,calm:!!options.calm});
  const page = await context.newPage();
  await page.clock.setFixedTime(new Date('2026-01-01T20:42:00Z'));
  page.on('pageerror', error => errors.push(error.message));
  await page.route('**/*', route => {
    const url = new URL(route.request().url());
    if (url.origin === ORIGIN) {
      const file = url.pathname === '/' ? 'index.html' : url.pathname.slice(1);
      if (['index.html','app.js','core.js','catalog.js','style.css'].includes(file)) {
        return route.fulfill({status:200,contentType:file.endsWith('.js')?'text/javascript':file.endsWith('.css')?'text/css':'text/html',body:source(file,!options.noKey,options.logos)});
      }
      if (file === 'icon.png') return route.fulfill({status:200,contentType:'image/png',body:fs.readFileSync(path.join(HOME,file))});
    }
    const voicePath = url.pathname.match(/^\/([a-f0-9]{64})\/voice\/(0|[1-9][0-9]{0,9})$/);
    if (url.origin === 'http://' + SPARK + ':4181' && voicePath && voicePath[1] === KEY) {
      const since = Number(voicePath[2]); voice.requests.push(since);
      // As on Spark: hold only while since is the current seq; one waiter at a time.
      if (since === 0 || since !== voice.snapshot.seq || voice.raw !== null) return voice.reply(route);
      voice.release();
      return new Promise(resolve => voice.waiters.push(() => { voice.reply(route); resolve(); }));
    }
    if (url.origin === 'http://' + SPARK + ':4181' && url.pathname.startsWith('/' + KEY + '/')) {
      const value = url.pathname.endsWith('/status') ? {state:'idle'} : options.weather || {state:'live',zip:'',temperatureF:64,condition:'Clear',icon:'clear',observedAt:'2026-01-01T20:42:00Z'};
      return route.fulfill({status:200,contentType:'application/json',headers:{'Access-Control-Allow-Origin':ORIGIN},body:JSON.stringify(value)});
    }
    throw Error('UNEXPECTED_NETWORK_REQUEST');
  });
  await page.goto(ORIGIN);
  await page.locator('#dock [data-app-id="netflix"]').first().waitFor();
  if (!options.noKey) {
    await page.locator('#weather-label').filter({hasText:options.weather ? '64°' : '64° Clear'}).waitFor();
    await page.waitForFunction(() => document.querySelector('.browser-status').textContent === 'Available');
  }
  // Release any held long-poll so closing never waits on a parked route.
  const close = context.close.bind(context);
  context.close = () => { voice.release(); return close(); };
  return {context,page,voice};
}
async function key(page, code) {
  return page.evaluate(code => document.dispatchEvent(new KeyboardEvent('keydown',{bubbles:true,cancelable:true,keyCode:code,which:code})),code);
}
const BADGE_WORDS = {confirmed:'Confirmed',sent:'Sent',unconfirmed:'Not confirmed',nothing:'Nothing done',changed:'Nothing changed',failed:'Didn’t work',waiting:'Waiting for you'};
const CHIP_COPY = {ready:'Hold the mic button to talk',warming:'Voice warming up · about a minute',starting:'Voice is starting up',unavailable:'Voice unavailable right now',paused:'Voice paused · restart it on Spark',offline:'Voice offline · can’t reach Spark'};
// One view per phase and per receipt family, with the longest closed headlines and details.
const VOICE_STATES = [
  {phase:'pressed',tone:'work',word:'GETTING READY',headline:'One moment...',detail:'Keep holding the mic button.',mood:'front'},
  {phase:'listening',tone:'listen',word:'LISTENING',headline:'Listening...',detail:'Speak, then let go.',meter:'hold',meterMs:5500,mood:'listening'},
  {phase:'listening',tone:'listen',word:'CONFIRMING',headline:'Listening...',detail:'Say “yes” or “no”, then let go.',meter:'hold',meterMs:5500,mood:'listening'},
  {phase:'released',tone:'work',word:'THINKING',headline:'Got it.',detail:'That was the 9-second limit. Using what I heard.',meter:'sweep',mood:'thinking'},
  {phase:'doing',tone:'work',word:'ON IT',headline:'Opening DuckDuckGo in the browser...',meter:'sweep',mood:'thinking'},
  {phase:'confirm',tone:'ask',word:'NEEDS YOUR OK',headline:'Turn the volume up 5?',detail:'Hold the mic button and say “yes” or “no”.',badge:'waiting',meter:'countdown',meterMs:14000,dwellMs:15000,mood:'asking'},
  {phase:'result',tone:'done',word:'DONE',headline:'Turned the volume down 5.',badge:'confirmed',dwellMs:4000,mood:'verified'},
  {phase:'result',tone:'sent',word:'SENT',headline:'Turning the volume down 5...',detail:'Watch the TV’s volume bar.',badge:'sent',dwellMs:4000,mood:'idle'},
  {phase:'result',tone:'unsure',word:'NOT SURE',headline:'Not sure that worked.',detail:'Check the TV before you try again.',badge:'unconfirmed',dwellMs:6000,mood:'asking'},
  {phase:'result',tone:'nothing',word:'NOTHING DONE',headline:'I’m sorry, I cannot do that.',badge:'nothing',dwellMs:5000,mood:'dim'},
  {phase:'result',tone:'failed',word:'DIDN’T WORK',headline:'R2D2 can’t reach the TV controls.',detail:'Nothing was changed. Try again in a moment.',badge:'failed',dwellMs:5000,mood:'dim'},
  {phase:'result',tone:'nothing',word:'NOTHING DONE',headline:'The R2D2 browser isn’t open.',detail:'Say “Open YouTube in the browser” first.',badge:'nothing',dwellMs:5000,mood:'idle'},
  {phase:'result',tone:'nothing',word:'NOTHING DONE',headline:'Cancelled.',detail:'Nothing was changed.',badge:'changed',dwellMs:5000,mood:'idle'},
  {phase:'result',tone:'nothing',word:'WARMING UP',headline:'Voice is still warming up.',detail:'It needs about a minute after the TV wakes.',badge:'nothing',dwellMs:5000,mood:'sleepy'},
  {phase:'result',tone:'nothing',word:'NOTHING DONE',headline:'Voice isn’t available right now.',detail:'Nothing was done. Try again in a few minutes.',badge:'nothing',dwellMs:5000,mood:'dim'},
  // Spark's longest closed headline, and a failure word exactly as Spark sends it (ASCII apostrophe).
  {phase:'confirm',tone:'ask',word:'NEEDS YOUR OK',headline:'Open DuckDuckGo in the browser again?',detail:'Hold the mic button and say “yes” or “no”.',badge:'waiting',meter:'countdown',meterMs:14000,dwellMs:15000,mood:'asking'},
  {phase:'result',tone:'failed',word:"DIDN'T WORK",headline:'The TV refused that.',detail:'Nothing changed.',badge:'failed',dwellMs:5000,mood:'idle'}
];
const FOCUSABLE = 'button,input,select,textarea,a[href],[tabindex],[contenteditable]';
// Home re-polls with since=N only after rendering N, so that request is the render barrier.
async function shownVoice(fx, headline, where = 'stage') {
  const seq = fx.voice.snapshot.seq, page = fx.page;
  for (let i = 0; i < 150 && !fx.voice.requests.includes(seq); i++) await page.waitForTimeout(20);
  assert.ok(fx.voice.requests.includes(seq),'Home never acknowledged voice seq ' + seq);
  await page.waitForFunction(([text,where]) => {
    const view = document.getElementById('voice-' + where), line = document.getElementById(where === 'stage' ? 'voice-headline' : 'voice-card-headline');
    return !view.hidden && line.textContent === text && !view.classList.contains('voice-leaving');
  },[headline,where]);
  await page.waitForTimeout(220);
  // A fixed pause can undercut the 180 ms enter spring on a slow runner, so
  // geometry is read only after the view's own finite animations finish.
  await page.evaluate(where => Promise.all(document.getElementById('voice-' + where).getAnimations()
    .filter(a => a.effect && a.effect.getComputedTiming().iterations !== Infinity)
    .map(a => a.finished.catch(() => {}))),where);
}
async function voiceLayout(page) {
  return page.evaluate(focusable => {
    const rect = el => { const r = el.getBoundingClientRect(); return {left:r.left,top:r.top,right:r.right,bottom:r.bottom,width:r.width,height:r.height}; };
    const stage = document.getElementById('voice-stage'), card = document.getElementById('voice-card'), badge = document.getElementById('voice-badge'), meter = document.getElementById('voice-meter');
    return {stage:rect(stage),card:rect(card),hero:rect(document.querySelector('.hero')),apps:rect(document.querySelector('.apps-section')),
      parts:Array.from(stage.children).filter(el => el.getClientRects().length).map(el => ({...rect(el),clipped:el.scrollWidth > el.clientWidth})),
      word:document.getElementById('voice-word').textContent,headline:document.getElementById('voice-headline').textContent,
      detail:document.getElementById('voice-detail').hidden ? '' : document.getElementById('voice-detail').textContent,
      badge:badge.hidden ? null : badge.textContent,badgeKind:badge.dataset.badge || null,
      meter:meter.hidden ? null : meter.dataset.meter,meterAnimation:meter.firstElementChild ? getComputedStyle(meter.firstElementChild).animationName : null,
      mood:document.querySelector('.r2-art').dataset.voice || 'idle',active:document.body.classList.contains('voice-active'),
      heroVisibility:getComputedStyle(document.querySelector('.hero')).visibility,
      focusable:document.querySelectorAll(['#voice-stage','#voice-card','#voice-chip'].map(id => focusable.split(',').map(sel => id + ' ' + sel).join(',')).join(',')).length,
      active_element:document.activeElement && (document.activeElement.dataset.appId || document.activeElement.id)};
  },FOCUSABLE);
}
const within = (inner, outer) => inner.left >= outer.left - .5 && inner.top >= outer.top - .5 && inner.right <= outer.right + .5 && inner.bottom <= outer.bottom + .5;
async function run() {
  const browser = await chromium.launch({headless:true});
  try {
    const {context,page} = await fixture(browser);
    const ids = await page.locator('#dock [data-app-id]').evaluateAll(items => items.map(item => item.dataset.appId));
    assert.equal(ids.length,10); assert.equal(new Set(ids).size,ids.length);
    assert.equal(await page.locator('#dock button').count(),12);
    assert.equal(await page.locator('#chromium-browser').count(),1);
    assert.equal(await page.locator('[data-app-id="local.owner.r2d2mic"]').count(),0);
    assert.equal(await page.locator('#rail, #hero-launch, #hero-library').count(),0);
    // No logo ships: every favorite is a text-mark tile, in the dock and in Manage apps.
    for (const name of fs.readdirSync(HOME).filter(name => name.endsWith('.in'))) assert.doesNotMatch(fs.readFileSync(path.join(HOME,name),'utf8'),/data:image|base64/,name);
    assert.equal(source('catalog.js').split('"logos":{}').length,2);
    const marks = await page.evaluate(() => [window.OwnerCatalog.logos,window.OwnerCatalog.favorites.filter(brand => 'logo' in brand).length,
      Object.fromEntries(window.OwnerCatalog.favorites.map(brand => [brand.id,brand.mark]))]);
    assert.deepEqual(marks.slice(0,2),[{},0]);
    assert.equal(await page.locator('#dock img, .brand-image').count(),0);
    assert.deepEqual(Object.fromEntries(await page.locator('#dock [data-app-id]').evaluateAll(items => items.map(item => [item.dataset.appId,item.querySelector('.tile').textContent]))),marks[2]);
    assert.equal(await page.locator('#chromium-browser .icon-browser').count(),1);
    groups++;

    // Real geometry navigation across both rows, OK launch, and Back restoration.
    assert.equal(await page.locator('#dock [data-app-id="netflix"]').evaluate(el => el === document.activeElement),true);
    await page.keyboard.press('ArrowRight');
    assert.equal(await page.evaluate(() => document.activeElement.dataset.appId),'com.apple.appletv');
    await page.keyboard.press('ArrowDown');
    assert.equal(await page.evaluate(() => document.activeElement.dataset.appId),'com.wbd.stream');
    await page.keyboard.press('ArrowUp');
    assert.equal(await page.evaluate(() => document.activeElement.dataset.appId),'com.apple.appletv');
    await page.keyboard.press('Enter');
    await page.waitForFunction(() => window.fixtureLaunches.length === 1);
    assert.deepEqual(await page.evaluate(() => window.fixtureLaunches[0]),{id:'com.apple.appletv'});
    await page.locator('#all-apps').click();
    await page.waitForFunction(() => !document.getElementById('overlay').hidden);
    await key(page,461);
    assert.equal(await page.locator('#overlay').evaluate(el => el.hidden),true);
    assert.equal(await page.evaluate(() => document.activeElement.id),'all-apps');
    groups++;

    // Pointer selection updates context without launching; one click launches once.
    await page.locator('#dock [data-app-id="hulu"]').hover();
    assert.equal(await page.locator('#hero-brand').textContent(),'HULU');
    assert.equal(await page.evaluate(() => window.fixtureLaunches.length),1);
    await page.locator('#dock [data-app-id="hulu"]').click();
    await page.waitForFunction(() => window.fixtureLaunches.length === 2);
    assert.equal(await page.evaluate(() => window.fixtureLaunches[1].id),'hulu');
    groups++;

    await page.locator('#chromium-browser').focus();
    await page.waitForFunction(() => document.querySelector('.browser-status').textContent === 'Available');
    assert.equal(await page.locator('#chromium-browser .dock-label').textContent(),'R2D2 Browser');
    assert.equal(await page.locator('#chromium-browser').getAttribute('aria-label'),'R2D2 Browser — Available');
    assert.equal(await page.locator('#hero-brand').textContent(),'R2D2 BROWSER');
    assert.equal(await page.locator('#hero-kicker').textContent(),'POWERED BY CHROMIUM');
    await page.locator('#chromium-browser').click();
    await page.waitForFunction(() => window.fixtureLaunches.length === 3);
    assert.deepEqual(await page.evaluate(() => window.fixtureLaunches[2]),{id:'com.webos.app.tinybrowser',params:{contentTarget:'http://' + SPARK + ':4181/' + KEY + '/open'}});
    groups++;

    // AirPlay remains reachable in Inputs; browser remains on its fixed route.
    await page.locator('#inputs-open').click();
    assert.equal(await page.locator('#panel-title').textContent(),'Inputs & AirPlay');
    assert.equal(await page.locator('#panel-content [data-app-id]').count(),5);
    await page.locator('#panel-content [data-app-id="airplay"]').click();
    await page.waitForFunction(() => window.fixtureLaunches.length === 4);
    assert.equal(await page.evaluate(() => window.fixtureLaunches[3].id),'airplay');
    await key(page,461);
    groups++;

    await page.locator('#search-open').click();
    await page.locator('#search-input').fill('apple');
    assert.equal(await page.locator('#panel-content button').count(),2);
    await page.keyboard.press('ArrowDown');
    assert.equal(await page.evaluate(() => document.activeElement.dataset.appId),'com.apple.appletv');
    await page.keyboard.press('Tab');
    assert.equal(await page.locator('#overlay').evaluate(el => el.contains(document.activeElement)),true);
    await page.keyboard.press('Escape');
    assert.equal(await page.evaluate(() => document.activeElement.id),'search-open');
    for (const code of [447,448,449,1005]) assert.equal(await key(page,code),true,'TV/voice key must be left unhandled');
    groups++;

    // Full-screen layout and static hit boxes stay intact despite inner spring motion.
    for (const size of [{width:1920,height:1080},{width:3840,height:2160}]) {
      await page.setViewportSize(size);
      await page.waitForTimeout(220);
      const layout = await page.evaluate(() => {
        const rect = el => { const r=el.getBoundingClientRect(); return {left:r.left,top:r.top,right:r.right,bottom:r.bottom,width:r.width,height:r.height}; };
        return {screen:rect(document.getElementById('screen')),hero:rect(document.querySelector('.hero')),apps:rect(document.querySelector('.apps-section')),buttons:Array.from(document.querySelectorAll('#dock button')).map(rect),top:rect(document.querySelector('.topbar'))};
      });
      assert.equal(Math.round(layout.screen.width),size.width); assert.equal(Math.round(layout.screen.height),size.height);
      assert.ok(layout.hero.bottom < layout.apps.top);
      for (const b of layout.buttons) assert.ok(b.left>=0 && b.top>=0 && b.right<=size.width && b.bottom<=size.height,'App hit target clipped');
      assert.ok(layout.top.right <= size.width);
      const a = await page.locator('#dock button').first().boundingBox();
      await page.locator('#dock button').first().focus(); await page.waitForTimeout(220);
      assert.deepEqual(await page.locator('#dock button').first().boundingBox(),a,'Focus must not move remote navigation hit box');
    }
    groups++;

    if (process.argv[2]) {
      const dir=path.resolve(process.argv[2]);fs.mkdirSync(dir,{recursive:true});
      await page.setViewportSize({width:1920,height:1080});
      await page.reload();
      await page.waitForFunction(() => document.querySelector('.browser-status').textContent === 'Available');
      await page.locator('#dock [data-app-id="netflix"]').focus();
      await page.waitForTimeout(230);
      await page.screenshot({path:path.join(dir,'r2d2-home-proposal-1920.png')});
      await page.locator('#chromium-browser').focus();
      await page.waitForFunction(() => document.querySelector('.browser-status').textContent === 'Available');
      await page.waitForTimeout(230);
      await page.screenshot({path:path.join(dir,'r2d2-browser-proposal-1920.png')});
      await page.locator('#chromium-browser').screenshot({path:path.join(dir,'r2d2-browser-tile.png')});
    }
    await context.close();

    const reduced = await fixture(browser,{reducedMotion:'reduce'});
    await reduced.page.locator('#chromium-browser').focus();
    assert.deepEqual(await reduced.page.locator('#chromium-browser .tile').evaluate(el => {const s=getComputedStyle(el);return [s.transitionDuration,s.transform];}),['0s','none']);
    assert.equal(await reduced.page.locator('.r2-idle').evaluate(el => getComputedStyle(el).animationName),'none');
    await reduced.page.locator('#all-apps').click();
    assert.equal(await reduced.page.locator('.panel').evaluate(el => getComputedStyle(el).animationName),'none');
    await reduced.context.close();groups++;

    const sparse = await fixture(browser,{inventory:['netflix','com.webos.app.tinybrowser','airplay','com.webos.app.hdmi1']});
    assert.equal(await sparse.page.locator('#dock button').count(),3);
    assert.deepEqual(await sparse.page.locator('#dock [data-app-id]').evaluateAll(items=>items.map(item=>item.dataset.appId)),['netflix']);
    await sparse.page.locator('#inputs-open').click();
    assert.equal(await sparse.page.locator('#panel-content [data-app-id]').count(),2);
    await sparse.context.close();groups++;
    // Logos come only from the build's private slot; unlisted favorites keep their text mark.
    const logos = {netflix:svgLogo('#e3bd92'),hulu:svgLogo('#83ecff')};
    const logoed = await fixture(browser,{logos});
    assert.deepEqual(await logoed.page.locator('#dock img').evaluateAll(items => items.map(item => [item.closest('[data-app-id]').dataset.appId,item.complete && item.naturalWidth === 40,item.getAttribute('src')])),[['netflix',true,logos.netflix],['hulu',true,logos.hulu]]);
    assert.equal(await logoed.page.locator('#dock [data-app-id="com.apple.appletv"] .tile').textContent(),'Apple TV');
    await logoed.page.locator('#preferences-open').click(); await logoed.page.locator('#manage-apps').click();
    assert.equal(await logoed.page.locator('[data-manage-id="netflix"] .manage-icon img, [data-manage-id="hulu"] .manage-icon img').count(),2);
    assert.equal(await logoed.page.locator('[data-manage-id="com.apple.appletv"] .manage-icon').textContent(),'Apple TV');
    await logoed.context.close();groups++;
    // Explicit management flow: Cancel is the default; Back never deletes.
    const managed = await fixture(browser,{protectedApp:'com.apple.appletv'});
    await managed.page.locator('#preferences-open').click();
    assert.equal(await managed.page.evaluate(()=>window.fixtureRemovals.length),0);
    assert.equal(await managed.page.evaluate(()=>window.fixtureLaunches.length),0);
    if(process.argv[2]) await managed.page.screenshot({path:path.join(path.resolve(process.argv[2]),'r2d2-home-settings.png')});
    await managed.page.locator('#manage-apps').click();
    assert.equal(await managed.page.locator('[data-manage-id="com.apple.appletv"] [data-delete-id]').count(),0);
    assert.equal(await managed.page.locator('[data-delete-id="com.webos.app.tinybrowser"], [data-delete-id="local.owner.r2d2mic"], [data-delete-id="local.owner.applibrary"]').count(),0);
    await managed.page.locator('[data-delete-id="netflix"]').click();
    assert.equal(await managed.page.evaluate(()=>document.activeElement.id),'delete-cancel');
    assert.match(await managed.page.locator('#panel-description').textContent(),/saved data may be removed/);
    await managed.page.keyboard.press('Enter');
    assert.equal(await managed.page.locator('#panel-title').textContent(),'Manage apps');
    assert.equal(await managed.page.evaluate(()=>window.fixtureRemovals.length),0);
    await managed.page.locator('[data-delete-id="netflix"]').click();
    await key(managed.page,461);
    assert.equal(await managed.page.locator('#panel-title').textContent(),'Manage apps');
    assert.equal(await managed.page.evaluate(()=>window.fixtureRemovals.length),0);
    if(process.argv[2]) await managed.page.screenshot({path:path.join(path.resolve(process.argv[2]),'r2d2-manage-apps.png')});
    await managed.page.locator('[data-delete-id="netflix"]').click();
    if(process.argv[2]) await managed.page.screenshot({path:path.join(path.resolve(process.argv[2]),'r2d2-delete-confirmation.png')});
    await managed.page.locator('#delete-confirm').click();
    await managed.page.waitForFunction(()=>document.querySelector('#toast').textContent==='Netflix deleted.');
    assert.deepEqual(await managed.page.evaluate(()=>window.fixtureRemovals),[{id:'netflix'}]);
    assert.equal(await managed.page.locator('#dock [data-app-id="netflix"], [data-delete-id="netflix"]').count(),0);
    assert.equal(await managed.page.locator('#panel-title').textContent(),'Manage apps');
    await key(managed.page,461);
    assert.equal(await managed.page.locator('#panel-title').textContent(),'Home preferences');
    assert.equal(await managed.page.evaluate(()=>window.fixtureLaunches.length),0);
    await managed.page.locator('#app-store').click();
    await managed.page.waitForFunction(()=>window.fixtureLaunches.length===1);
    assert.deepEqual(await managed.page.evaluate(()=>window.fixtureLaunches[0]),{id:'com.webos.app.discovery'});
    await managed.context.close();groups++;

    // A real negative native reply stays an error, never a successful deletion.
    const denied=await fixture(browser,{deleteMode:'denied'});
    await denied.page.locator('#preferences-open').click(); await denied.page.locator('#manage-apps').click();
    await denied.page.locator('[data-delete-id="netflix"]').click(); await denied.page.locator('#delete-confirm').click();
    await denied.page.locator('#panel-feedback').filter({hasText:'did not allow app deletion'}).waitFor();
    assert.equal(await denied.page.locator('#dock [data-app-id="netflix"]').count(),1);
    assert.equal(await denied.page.evaluate(()=>document.activeElement.id),'delete-cancel');
    assert.equal(await denied.page.locator('#delete-confirm').isDisabled(),true);
    assert.equal(await denied.page.locator('#delete-refresh').isVisible(),true);
    assert.deepEqual(await denied.page.evaluate(()=>window.fixtureRemovals),[{id:'netflix'}]);
    // Cancel/Back alone must recover synchronized inventory and launches; a
    // separate manual Refresh is not required after a failed mutation.
    await denied.page.locator('#delete-cancel').click();
    await denied.page.locator('[data-delete-id="netflix"]').waitFor();
    assert.equal(await denied.page.locator('#panel-title').textContent(),'Manage apps');
    await key(denied.page,461);await key(denied.page,461);
    await denied.page.locator('#dock [data-app-id="netflix"]').click();
    assert.equal(await denied.page.evaluate(()=>window.fixtureLaunches.length),1,'Cancel after failed deletion must restore app launch authority');
    await denied.page.waitForFunction(()=>document.querySelector('.browser-status').textContent==='Available');
    await denied.page.locator('#chromium-browser').click();
    await denied.page.waitForFunction(()=>window.fixtureLaunches.length===2);
    assert.equal(await denied.page.evaluate(()=>window.fixtureLaunches[0].id),'netflix');
    assert.equal(await denied.page.evaluate(()=>window.fixtureLaunches[1].id),'com.webos.app.tinybrowser');
    assert.deepEqual(await denied.page.evaluate(()=>window.fixtureRemovals),[{id:'netflix'}]);
    await denied.context.close();groups++;

    // Pending deletion cannot be dispatched twice; leaving does not invent success.
    const held=await fixture(browser,{deleteMode:'hold'});
    await held.page.locator('#preferences-open').click(); await held.page.locator('#manage-apps').click();
    await held.page.locator('[data-delete-id="netflix"]').click(); await held.page.locator('#delete-confirm').click();
    await held.page.waitForFunction(()=>window.fixtureRemovals.length===1);
    assert.equal(await held.page.locator('#delete-confirm').isDisabled(),true);
    assert.equal(await held.page.locator('#delete-cancel').textContent(),'Back to apps');
    await held.page.locator('#delete-cancel').click();
    assert.equal(await held.page.locator('[data-delete-id="netflix"]').isDisabled(),true);
    assert.equal(await held.page.evaluate(()=>document.activeElement.id),'panel-close','pending Back must focus an enabled modal control');
    assert.equal(await held.page.evaluate(()=>document.getElementById('overlay').contains(document.activeElement)&&!document.activeElement.disabled),true);
    await held.page.evaluate(()=>{window.dispatchEvent(new PageTransitionEvent('pagehide'));window.dispatchEvent(new PageTransitionEvent('pageshow'));});
    await held.page.locator('[data-delete-id="netflix"]:not(:disabled)').waitFor();
    assert.match(await held.page.locator('#toast').textContent(),/App deletion was interrupted/);
    await held.page.evaluate(()=>window.fixtureResolveRemoval());
    assert.equal(await held.page.locator('#dock [data-app-id="netflix"]').count(),1);
    assert.deepEqual(await held.page.evaluate(()=>window.fixtureRemovals),[{id:'netflix'}]);
    assert.match(await held.page.locator('#toast').textContent(),/App deletion was interrupted/);
    assert.doesNotMatch(await held.page.locator('#toast').textContent(),/deleted\./);
    await held.context.close();groups++;

    // A rejection arriving after Back to apps must also refresh authority.
    const lateDenied=await fixture(browser,{deleteMode:'hold'});
    await lateDenied.page.locator('#preferences-open').click();await lateDenied.page.locator('#manage-apps').click();
    await lateDenied.page.locator('[data-delete-id="netflix"]').click();await lateDenied.page.locator('#delete-confirm').click();
    await lateDenied.page.waitForFunction(()=>window.fixtureRemovals.length===1);await key(lateDenied.page,461);
    await lateDenied.page.evaluate(()=>window.fixtureRejectRemoval());
    await lateDenied.page.locator('[data-delete-id="netflix"]:not(:disabled)').waitFor();
    await key(lateDenied.page,461);await key(lateDenied.page,461);
    await lateDenied.page.locator('#dock [data-app-id="netflix"]').click();
    assert.equal(await lateDenied.page.evaluate(()=>window.fixtureLaunches.length),1,'late deletion failure must refresh launch authority');
    assert.deepEqual(await lateDenied.page.evaluate(()=>window.fixtureRemovals),[{id:'netflix'}]);
    await lateDenied.context.close();groups++;

    // Pagehide while the acknowledged removal waits between inventory polls:
    // the old waiter must not erase a fresh pageshow inventory300ms later.
    const polling=await fixture(browser,{deleteMode:'ack-only'});
    await polling.page.locator('#preferences-open').click();await polling.page.locator('#manage-apps').click();
    await polling.page.locator('[data-delete-id="netflix"]').click();await polling.page.locator('#delete-confirm').click();
    await polling.page.waitForFunction(()=>window.fixtureRequests.filter(call=>call.uri.endsWith('/listApps')).length>=3);
    await polling.page.evaluate(()=>{window.dispatchEvent(new PageTransitionEvent('pagehide'));window.dispatchEvent(new PageTransitionEvent('pageshow'));});
    await polling.page.locator('[data-delete-id="netflix"]:not(:disabled)').waitFor();
    await polling.page.waitForTimeout(400);
    await key(polling.page,461);await key(polling.page,461);
    await polling.page.locator('#dock [data-app-id="netflix"]').click();
    assert.equal(await polling.page.evaluate(()=>window.fixtureLaunches.length),1,'stale removal poll cannot clear the pageshow inventory');
    assert.deepEqual(await polling.page.evaluate(()=>window.fixtureRemovals),[{id:'netflix'}]);
    assert.doesNotMatch(await polling.page.locator('#toast').textContent(),/deleted\./);
    await polling.context.close();groups++;

    const noStore=await fixture(browser,{inventory:['netflix','com.webos.app.tinybrowser']});
    await noStore.page.locator('#preferences-open').click(); await noStore.page.locator('#app-store').click();
    await noStore.page.locator('#toast').filter({hasText:'App Store is not available'}).waitFor();
    assert.equal(await noStore.page.evaluate(()=>window.fixtureLaunches.length),0);
    await noStore.context.close();groups++;
    // Voice stage: every phase and receipt renders Spark's copy verbatim inside the hero rect.
    for (const size of [{width:1920,height:1080},{width:3840,height:2160}]) {
      const staged = await fixture(browser,{viewport:size});
      const scale = size.width / 1920;
      await staged.page.locator('#voice-chip').filter({hasText:CHIP_COPY.ready}).waitFor();
      assert.equal(await staged.page.evaluate(() => document.activeElement.dataset.appId),'netflix');
      const dockBoxes = () => staged.page.locator('#dock button, .topbar button').evaluateAll(items => items.map(item => item.getBoundingClientRect().toJSON()));
      const idleDock = await dockBoxes();
      for (const [index, state] of VOICE_STATES.entries()) {
        pushVoice(staged.voice,state);
        await shownVoice(staged,state.headline);
        if (process.argv[2] && size.width === 1920) await staged.page.screenshot({path:path.join(path.resolve(process.argv[2]),'r2d2-voice-' + String(index).padStart(2,'0') + '-' + state.phase + '.png')});
        const layout = await voiceLayout(staged.page), label = state.phase + ' ' + state.headline;
        assert.deepEqual([layout.word,layout.headline,layout.detail],[state.word,state.headline,state.detail || ''],label);
        assert.equal(layout.badge,state.badge ? BADGE_WORDS[state.badge] : null,label);
        assert.equal(layout.meter,state.meter || null,label);
        if (state.meter) assert.equal(layout.meterAnimation,{hold:'voice-fill',sweep:'voice-sweep',countdown:'voice-drain'}[state.meter],label);
        assert.equal(layout.mood,state.mood,label);
        assert.equal(layout.active,true); assert.equal(layout.heroVisibility,'hidden');
        assert.deepEqual(layout.stage,layout.hero,'stage must cover exactly the hero rect');
        assert.deepEqual([layout.stage.left,layout.stage.top,layout.stage.width,layout.stage.height],[92*scale,186*scale,1000*scale,337*scale]);
        for (const part of layout.parts) { assert.ok(within(part,layout.stage),label + ' escapes the hero rect'); assert.equal(part.clipped,false,label + ' is clipped'); }
        assert.ok(layout.stage.bottom < layout.apps.top);
        assert.equal(layout.focusable,0); assert.equal(layout.active_element,'netflix','voice must never take focus');
        assert.deepEqual(await dockBoxes(),idleDock,'voice must not move navigation hit boxes');
        if (state.mood === 'thinking') assert.ok(await staged.page.evaluate(() => parseFloat(getComputedStyle(document.querySelector('.r2-led')).animationDuration) >= 1 / 1.5),'thinking LEDs stay at or under 1.5 Hz');
      }
      await staged.context.close(); groups++;
    }

    // Focus, Back and keys behave as without voice; the stage leaves on its own dwell and the hero is current.
    const steady = await fixture(browser);
    pushVoice(steady.voice,VOICE_STATES[1]);
    await shownVoice(steady,'Listening...');
    await steady.page.keyboard.press('ArrowRight');
    assert.equal(await steady.page.evaluate(() => document.activeElement.dataset.appId),'com.apple.appletv');
    assert.equal(await key(steady.page,461),false);
    assert.equal(await steady.page.evaluate(() => document.activeElement.dataset.appId),'netflix');
    for (const code of [447,448,449,1005]) assert.equal(await key(steady.page,code),true,'TV/voice key must be left unhandled');
    assert.equal(await steady.page.locator('#voice-stage').isVisible(),true,'Back must not dismiss the stage');
    await steady.page.locator('#dock [data-app-id="hulu"]').focus();
    assert.equal(await steady.page.locator('#hero-brand').textContent(),'HULU');
    pushVoice(steady.voice,{phase:'result',tone:'done',word:'DONE',headline:'Muted.',badge:'confirmed',dwellMs:1500,mood:'verified'});
    await shownVoice(steady,'Muted.');
    await steady.page.waitForFunction(() => document.getElementById('voice-stage').hidden && !document.body.classList.contains('voice-active'),null,{timeout:4000});
    await steady.page.waitForTimeout(220);
    assert.equal(await steady.page.locator('.hero').evaluate(el => [getComputedStyle(el).visibility,getComputedStyle(el).opacity].join()),'visible,1');
    assert.equal(await steady.page.locator('#hero-brand').textContent(),'HULU');
    assert.equal(await steady.page.locator('#hero-tags > span').first().textContent(),'Say “Open Hulu”');
    assert.equal(await steady.page.evaluate(() => document.querySelector('.r2-art').dataset.voice),undefined);
    assert.equal(await steady.page.evaluate(() => document.activeElement.dataset.appId),'hulu');
    await steady.context.close(); groups++;

    // Per-tile hints are the first hero tag, without a stray separator inside them.
    const hinted = await fixture(browser);
    const hints = {netflix:'Open Netflix','com.apple.appletv':'Open Apple TV','com.tubitv.ott.tubi':'Open Tubi','youtube.leanback.v4':'Open the YouTube app',amazon:'Open Prime Video','com.disney.disneyplus-prod':'Open Disney Plus','com.wbd.stream':'Launch Max',hulu:'Open Hulu','com.movenetworks.app.sling-tv-sling-production':'Open Sling TV','com.apple.applemusic':'Launch Apple Music'};
    for (const [id,phrase] of Object.entries(hints)) {
      await hinted.page.locator('#dock [data-app-id="' + id + '"]').focus();
      const tags = await hinted.page.locator('#hero-tags > span').evaluateAll(items => items.map(item => [item.className,item.textContent,getComputedStyle(item,'::before').content]));
      assert.deepEqual(tags[0],['voice-hint','Say “' + phrase + '”','none'],id);
      assert.equal(tags[1][2],'"·"',id);
      assert.equal(await hinted.page.locator('#hero-tags .voice-hint span').count(),0);
    }
    await hinted.page.locator('#chromium-browser').focus();
    await hinted.page.waitForFunction(() => document.querySelector('.browser-status').textContent === 'Available');
    assert.deepEqual(await hinted.page.locator('#hero-tags > span').allTextContents(),['Say “Search YouTube for lo-fi music”','Private session','Available']);
    await hinted.page.locator('#all-apps').focus();
    assert.equal(await hinted.page.locator('#hero-brand').textContent(),'R2D2 BROWSER','All apps has no hint of its own');
    const tagBox = await hinted.page.locator('#hero-tags').boundingBox(), heroBox = await hinted.page.locator('.hero').boundingBox();
    assert.ok(tagBox.y + tagBox.height <= heroBox.y + heroBox.height);
    await hinted.context.close(); groups++;

    // Chip: absolute at x488 in the topbar, a status (not a control), and every copy clears the right group.
    const chipped = await fixture(browser);
    for (const chip of ['ready','warming','starting','unavailable','paused']) {
      pushVoice(chipped.voice,{chip});
      await chipped.page.locator('#voice-chip').filter({hasText:CHIP_COPY[chip]}).waitFor();
      const box = await chipped.page.evaluate(() => { const r = el => el.getBoundingClientRect(), chip = document.getElementById('voice-chip'), label = document.getElementById('voice-chip-label');
        return {chip:r(chip).toJSON(),right:r(document.querySelector('.topbar-right')).left,role:chip.getAttribute('role'),clipped:label.scrollWidth > label.clientWidth,state:chip.dataset.chip,size:getComputedStyle(label).fontSize}; });
      assert.deepEqual([box.chip.left,box.chip.top,box.chip.height,box.role,box.state,box.size],[488,52,58,'status',chip,'24px']);
      assert.equal(box.clipped,false,chip); assert.ok(box.chip.right <= 960 && box.chip.right < box.right - 20,chip + ' chip collides with the topbar');
    }
    await chipped.context.close(); groups++;

    // A panel turns the stage into the compact card; focus stays in the panel and Back still closes it.
    for (const size of [{width:1920,height:1080},{width:3840,height:2160}]) {
      const paneled = await fixture(browser,{viewport:size}), scale = size.width / 1920;
      await paneled.page.locator('#profile-open').click();
      pushVoice(paneled.voice,VOICE_STATES[1]);
      await shownVoice(paneled,'Listening...','card');
      let card = await paneled.page.evaluate(() => { const el = document.getElementById('voice-card'), s = getComputedStyle(el); return {rect:el.getBoundingClientRect().toJSON(),z:s.zIndex,events:s.pointerEvents,stage:document.getElementById('voice-stage').hidden,detail:document.getElementById('voice-card-detail').textContent,inPanel:document.getElementById('overlay').contains(document.activeElement)}; });
      assert.deepEqual([card.rect.left,card.rect.top,card.rect.width,card.rect.height],[480*scale,54*scale,960*scale,156*scale]);
      assert.deepEqual([card.z,card.events,card.stage,card.detail,card.inPanel],['21','none',true,'Speak, then let go.',true]);
      pushVoice(paneled.voice,{phase:'result',tone:'unsure',word:'NOT SURE',headline:'Not sure that worked.',badge:'unconfirmed',dwellMs:6000,mood:'asking'});
      await shownVoice(paneled,'Not sure that worked.','card');
      card = await paneled.page.evaluate(() => { const el = document.getElementById('voice-card'), badge = document.getElementById('voice-card-badge'), r = el.getBoundingClientRect(), b = badge.getBoundingClientRect(); return {rect:r.toJSON(),badge:badge.textContent,inside:b.right <= r.right && b.left >= r.left,clipped:document.getElementById('voice-card-headline').scrollWidth > document.getElementById('voice-card-headline').clientWidth}; });
      assert.deepEqual([card.rect.height,card.badge,card.inside,card.clipped],[112*scale,'Not confirmed',true,false]);
      assert.equal(await paneled.page.evaluate(sel => document.querySelectorAll(sel.split(',').map(s => '#voice-card ' + s).join(',')).length,FOCUSABLE),0);
      await key(paneled.page,461);
      await shownVoice(paneled,'Not sure that worked.');
      assert.deepEqual(await paneled.page.evaluate(() => [document.getElementById('overlay').hidden,document.getElementById('voice-card').hidden,document.activeElement.id]),[true,true,'profile-open']);
      // Opening a panel over a live stage moves the same receipt into the card at once.
      const requests = paneled.voice.requests.length;
      await paneled.page.keyboard.press('Enter');
      assert.deepEqual(await paneled.page.evaluate(() => [document.getElementById('panel-title').textContent,document.getElementById('voice-stage').hidden,document.getElementById('voice-card').hidden,document.getElementById('voice-card-headline').textContent,document.body.classList.contains('voice-active')]),['Remote & help',true,false,'Not sure that worked.',false]);
      assert.equal(paneled.voice.requests.length,requests,'the card needs no new reply');
      await paneled.context.close(); groups++;
    }

    // Reduced motion: static end states only; the meter (a timer) is not drawn frozen.
    const stillVoice = await fixture(browser,{reducedMotion:'reduce'});
    pushVoice(stillVoice.voice,VOICE_STATES[1]);
    await shownVoice(stillVoice,'Listening...');
    assert.deepEqual(await stillVoice.page.evaluate(() => { const s = el => getComputedStyle(document.querySelector(el)); return [s('#voice-stage').animationName,s('.r2-arc').animationName,s('.r2-arc').opacity,s('.r2-halo').opacity,s('#voice-meter').display,s('.r2-head').transitionDuration]; }),['none','none','0.9','1','none','0s']);
    pushVoice(stillVoice.voice,VOICE_STATES[3]);
    await shownVoice(stillVoice,'Got it.');
    assert.deepEqual(await stillVoice.page.evaluate(() => [getComputedStyle(document.querySelector('.r2-led')).animationName,getComputedStyle(document.querySelector('.r2-led-secondary')).opacity]),['none','1']);
    pushVoice(stillVoice.voice,{phase:'result',tone:'unsure',word:'NOT SURE',headline:'Not sure that worked.',badge:'unconfirmed',dwellMs:1500,mood:'asking'});
    await stillVoice.page.waitForFunction(() => document.querySelector('.r2-art').dataset.voice === 'asking');
    assert.match(await stillVoice.page.evaluate(() => getComputedStyle(document.querySelector('.r2-head')).transform),/^matrix\(0\.99/);
    await stillVoice.page.waitForFunction(() => document.getElementById('voice-stage').hidden,null,{timeout:4000});
    await stillVoice.context.close(); groups++;

    // Calm: moods drop to static states instead of freezing mid-frame; entry is a plain fade.
    const calmVoice = await fixture(browser,{calm:true});
    assert.equal(await calmVoice.page.evaluate(() => document.body.classList.contains('calm')),true);
    pushVoice(calmVoice.voice,VOICE_STATES[1]);
    await calmVoice.page.waitForFunction(() => document.getElementById('voice-headline').textContent === 'Listening...');
    assert.equal(await calmVoice.page.evaluate(() => getComputedStyle(document.getElementById('voice-stage')).animationName),'voice-fade');
    assert.deepEqual(await calmVoice.page.evaluate(() => ['.r2-idle','.r2-head','.r2-arc','.r2-led'].map(sel => getComputedStyle(document.querySelector(sel)).animationName)),['none','none','none','none']);
    assert.equal(await calmVoice.page.evaluate(() => getComputedStyle(document.querySelector('.r2-arc')).opacity),'0.9');
    pushVoice(calmVoice.voice,VOICE_STATES[4]);
    await shownVoice(calmVoice,VOICE_STATES[4].headline);
    assert.equal(await calmVoice.page.evaluate(() => getComputedStyle(document.querySelector('#voice-meter i')).animationName),'none');
    pushVoice(calmVoice.voice,{phase:'result',tone:'unsure',word:'NOT SURE',headline:'Not sure that worked.',badge:'unconfirmed',dwellMs:6000,mood:'asking'});
    await shownVoice(calmVoice,'Not sure that worked.');
    assert.deepEqual(await calmVoice.page.evaluate(() => { const s = getComputedStyle(document.querySelector('.r2-head')); return [s.transitionDuration,s.transform.slice(0,12)]; }),['0s','matrix(0.990']);
    await calmVoice.context.close(); groups++;

    // Invalid replies are failures: two in a row show the Home-local offline chip and drop the stale stage.
    const broken = await fixture(browser);
    pushVoice(broken.voice,VOICE_STATES[3]);
    await shownVoice(broken,'Got it.');
    const valid = JSON.parse(JSON.stringify(broken.voice.snapshot));
    broken.voice.raw = JSON.stringify({...valid,seq:valid.seq + 1,echo:'lo-fi music'});
    broken.voice.release();
    await broken.page.waitForTimeout(500);
    assert.equal(await broken.page.locator('#voice-chip-label').textContent(),CHIP_COPY.ready,'one failure is not offline');
    assert.equal(await broken.page.locator('#voice-stage').isVisible(),true);
    broken.voice.raw = JSON.stringify({...valid,seq:valid.seq + 1,headline:'Café'});
    await broken.page.locator('#voice-chip').filter({hasText:CHIP_COPY.offline}).waitFor({timeout:3000});
    assert.equal(await broken.page.evaluate(() => document.getElementById('voice-chip').dataset.chip),'offline');
    await broken.page.waitForFunction(() => document.getElementById('voice-stage').hidden && !document.body.classList.contains('voice-active'));
    assert.equal(await broken.page.locator('body').textContent().then(text => /lo-fi|Caf/.test(text)),false,'rejected text never renders');
    broken.voice.raw = null; pushVoice(broken.voice,{chip:'warming'});
    await broken.page.locator('#voice-chip').filter({hasText:CHIP_COPY.warming}).waitFor({timeout:4000});
    await broken.context.close(); groups++;

    // Replay: an old receipt is not shown on arrival, a fresh one is, and a seen seq never renders twice.
    const stale = await fixture(browser,{voice:{seq:5,phase:'result',ageMs:7000,tone:'done',word:'DONE',headline:'You’re home.',badge:'confirmed',dwellMs:20000,mood:'verified'}});
    await stale.page.locator('#voice-chip').filter({hasText:CHIP_COPY.ready}).waitFor();
    await stale.page.waitForTimeout(500);
    assert.deepEqual(await stale.page.evaluate(() => [document.getElementById('voice-stage').hidden,document.body.classList.contains('voice-active')]),[true,false]);
    assert.deepEqual(stale.voice.requests.slice(0,2),[0,5]);
    await stale.context.close();
    const fresh = await fixture(browser,{voice:{seq:5,phase:'result',ageMs:1000,tone:'done',word:'DONE',headline:'You’re home.',badge:'confirmed',dwellMs:20000,mood:'verified'}});
    const aborted = []; fresh.page.on('requestfailed',request => { if (request.url().includes('/voice/')) aborted.push(request.url()); });
    await shownVoice(fresh,'You’re home.');
    await fresh.page.evaluate(() => window.fixtureSetHidden(true));
    assert.deepEqual(await fresh.page.evaluate(() => [document.getElementById('voice-stage').hidden,document.body.classList.contains('voice-active'),document.querySelector('.r2-art').dataset.voice]),[true,false,undefined]);
    await fresh.page.waitForTimeout(400);
    assert.ok(aborted.length >= 1,'hiding must abort the held long-poll');
    const parked = fresh.voice.requests.length; await fresh.page.waitForTimeout(600);
    assert.equal(fresh.voice.requests.length,parked,'a hidden Home never polls');
    await fresh.page.evaluate(() => window.fixtureSetHidden(false));
    await fresh.page.waitForTimeout(400);
    assert.equal(fresh.voice.requests[parked],0,'a visible Home starts again from since=0');
    assert.equal(await fresh.page.locator('#voice-stage').isVisible(),false,'a seen seq never renders twice');
    await fresh.page.evaluate(() => { window.dispatchEvent(new PageTransitionEvent('pagehide')); });
    const hiddenAt = fresh.voice.requests.length; await fresh.page.waitForTimeout(500);
    assert.equal(fresh.voice.requests.length,hiddenAt,'pagehide stops the feed');
    await fresh.page.evaluate(() => { window.dispatchEvent(new PageTransitionEvent('pageshow')); });
    pushVoice(fresh.voice,{phase:'result',tone:'done',word:'DONE',headline:'Sound is back on.',badge:'confirmed',dwellMs:4000,mood:'verified'});
    await shownVoice(fresh,'Sound is back on.');
    await fresh.context.close(); groups++;

    // Refreshing apps cancels service reads only; the voice feed keeps its own requests.
    const refreshed = await fixture(browser);
    await refreshed.page.locator('#voice-chip').filter({hasText:CHIP_COPY.ready}).waitFor();
    for (let i = 0; i < 50 && !refreshed.voice.waiters.length; i++) await refreshed.page.waitForTimeout(20);
    const parkedPolls = refreshed.voice.requests.length;
    await refreshed.page.locator('#preferences-open').click();
    await refreshed.page.getByRole('button',{name:'Refresh apps'}).click();
    await refreshed.page.locator('#dock [data-app-id="netflix"]').first().waitFor();
    // A cancelled poll would come back as a failure and re-poll after its 1 s backoff.
    await refreshed.page.waitForTimeout(1300);
    assert.deepEqual([refreshed.voice.requests.length,refreshed.voice.waiters.length],[parkedPolls,1],'refresh must not cancel the held long-poll');
    pushVoice(refreshed.voice,VOICE_STATES[0]);
    await shownVoice(refreshed,'One moment...');
    assert.deepEqual(await refreshed.page.evaluate(() => window.fixtureStorage),[],'voice writes no storage');
    await refreshed.context.close(); groups++;

    // Without a browser key there is no feed, no chip, no hint and no voice help.
    const keyless = await fixture(browser,{noKey:true});
    await keyless.page.waitForTimeout(400);
    assert.deepEqual(keyless.voice.requests,[]);
    assert.deepEqual(await keyless.page.evaluate(() => [document.getElementById('voice-chip').hidden,document.querySelectorAll('.voice-hint').length]),[true,0]);
    await keyless.page.locator('#profile-open').click();
    assert.equal(await keyless.page.locator('#voice-help-open').count(),0);
    await keyless.context.close(); groups++;

    // Voice help: reachable from Remote & help, fits without scrolling, and Back returns to it.
    const helped = await fixture(browser);
    await helped.page.locator('#profile-open').click();
    assert.equal(await helped.page.evaluate(() => document.activeElement.id),'voice-help-open');
    assert.equal(await helped.page.evaluate(() => { const c = document.getElementById('panel-content'); return c.scrollHeight <= c.clientHeight; }),true,'profile panel must fit');
    await helped.page.keyboard.press('Enter');
    assert.equal(await helped.page.locator('#panel-title').textContent(),'Voice commands');
    const help = await helped.page.evaluate(() => { const c = document.getElementById('panel-content'), panel = document.getElementById('panel').getBoundingClientRect();
      return {fits:c.scrollHeight <= c.clientHeight,inside:panel.bottom <= innerHeight && panel.top >= 0,focus:document.activeElement.id,controls:c.querySelectorAll('button,input,a[href],[tabindex]').length,
        headings:Array.from(c.querySelectorAll('h3'),h => h.textContent),steps:Array.from(c.querySelectorAll('.voice-steps li'),li => li.textContent),
        notes:Array.from(c.querySelectorAll('.voice-help-note'),p => p.textContent),groups:Array.from(c.querySelectorAll('.voice-phrases b'),b => b.textContent),
        phrases:Array.from(c.querySelectorAll('.voice-phrases ul'),ul => Array.from(ul.children,li => li.textContent)),
        legend:Array.from(c.querySelectorAll('.voice-legend > span'),span => span.textContent),badges:Array.from(c.querySelectorAll('.voice-legend .voice-badge'),b => b.dataset.badge),
        clipped:Array.from(c.querySelectorAll('.voice-help li, .voice-help p, .voice-legend > span')).some(el => el.scrollWidth > el.clientWidth + 1)}; });
    assert.deepEqual([help.fits,help.inside,help.focus,help.controls,help.clipped],[true,true,'panel-close',0,false]);
    assert.deepEqual(help.headings,['How it works','What the receipts mean','Things you can say']);
    assert.deepEqual(help.steps,['Hold the mic button. Wait until you see Listening...','Say one short command. You have about 9 seconds.','Let go. R2D2 shows what it understood and whether it worked.']);
    assert.deepEqual(help.notes,['Speech is handled on Spark at home and isn’t saved.','Not yet: power, inputs, channels, settings, or typing into pages.']);
    assert.deepEqual(help.groups,['APPS','R2D2 BROWSER','WATCHING','SOUND','ANYWHERE']);
    assert.deepEqual(help.phrases,[['“Open Netflix”','“Launch Max”','“Open the YouTube app”'],['“Open YouTube in the browser”','“Search YouTube for lo-fi music”','“Search the web for news today”','“Go back”','“Close the browser”'],['“Pause”','“Play”','“What’s playing?”'],['“Turn it up”','“Turn it down”','“Mute”','“Unmute”'],['“Go home”','“Cancel that”']]);
    assert.deepEqual(help.badges,['confirmed','sent','unconfirmed','nothing','failed']);
    assert.deepEqual(help.legend,['Confirmed','R2D2 checked that it happened.','Sent','Sent, but the app doesn’t report back.','Not confirmed','Check the TV before you try again.','Nothing done','R2D2 didn’t change anything.','Didn’t work','The TV or browser refused it.']);
    if (process.argv[2]) await helped.page.screenshot({path:path.join(path.resolve(process.argv[2]),'r2d2-voice-help.png')});
    await key(helped.page,461);
    assert.deepEqual(await helped.page.evaluate(() => [document.getElementById('panel-title').textContent,document.activeElement.id]),['Remote & help','voice-help-open']);
    await key(helped.page,461);
    assert.equal(await helped.page.locator('#overlay').evaluate(el => el.hidden),true);
    await helped.context.close(); groups++;

    // Weather never names a place: Spark sends zip "" (an older Spark sent five digits); neither is shown.
    for (const zip of ['', '00000']) {
      const shown = await fixture(browser, {weather:{state:'live',zip,temperatureF:64,condition:'Clear',icon:'clear',observedAt:'2026-01-01T20:42:00Z'}});
      const weather = await shown.page.locator('#weather').evaluate(el => ({text:el.textContent,label:el.getAttribute('aria-label'),title:el.title}));
      assert.deepEqual(weather,{text:'64° Clear',label:'Weather: 64° Clear',title:'64° Clear'},zip);
      assert.equal(await shown.page.evaluate(() => /\d{5}/.test(document.body.innerText + document.getElementById('weather').outerHTML)),false,zip);
      await shown.context.close();
    }
    const missing = await fixture(browser, {noKey:true});
    await missing.page.waitForFunction(() => document.getElementById('weather').title === 'Weather unavailable');
    assert.deepEqual(await missing.page.locator('#weather').evaluate(el => [el.textContent,el.getAttribute('aria-label'),el.title]),['Weather unavailable','Weather unavailable','Weather unavailable']);
    await missing.context.close(); groups++;

    // Overflowing one-line labels end in three periods: the TV's fallback font draws the
    // U+2026 that text-overflow:ellipsis would insert mid-line.
    const longCondition = 'Thunderstorm in the vicinity with heavy rain and blowing dust';
    const cut = await fixture(browser, {weather:{state:'live',zip:'',temperatureF:64,condition:longCondition,icon:'storm',observedAt:'2026-01-01T20:42:00Z'}});
    const label = await cut.page.locator('#weather-label').evaluate(el => ({text:el.textContent,fits:el.scrollWidth <= el.clientWidth,overflow:getComputedStyle(el).textOverflow}));
    assert.ok(label.text.startsWith('64° Thunderstorm') && label.text.endsWith('...') && !label.text.includes('\u2026') && label.fits && label.overflow === 'clip', JSON.stringify(label));
    assert.equal(await cut.page.locator('#weather').getAttribute('title'),'64° ' + longCondition);
    assert.equal(await cut.page.locator('#weather').getAttribute('aria-label'),'Weather: 64° ' + longCondition);
    await cut.page.locator('#all-apps').click();
    await cut.page.waitForFunction(() => !document.getElementById('overlay').hidden);
    assert.deepEqual(await cut.page.evaluate(() => Array.from(document.querySelectorAll('*')).filter(el => getComputedStyle(el).textOverflow === 'ellipsis').map(el => el.id || el.className)),[]);
    await cut.context.close(); groups++;

    if (process.argv[2]) {
      const shots = await fixture(browser), dir = path.resolve(process.argv[2]);
      for (const [name,index] of [['listening',1],['thinking',3],['confirm',5],['done',6],['cannot',9]]) {
        pushVoice(shots.voice,{...VOICE_STATES[index],dwellMs:null}); await shownVoice(shots,VOICE_STATES[index].headline);
        await shots.page.screenshot({path:path.join(dir,'r2d2-voice-' + name + '.png')});
      }
      await shots.page.locator('#profile-open').click(); await shownVoice(shots,VOICE_STATES[9].headline,'card');
      await shots.page.screenshot({path:path.join(dir,'r2d2-voice-card.png')});
      await shots.context.close();
    }
    assert.deepEqual(errors,[]);
    console.log(JSON.stringify({groups,actualChromium:true,fixtureInventory:true,liveTvTested:false}));
  } finally { await browser.close(); }
}
run().catch(error => {console.error(error);process.exitCode=1;});
