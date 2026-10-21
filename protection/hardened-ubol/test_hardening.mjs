import assert from 'node:assert/strict';
import fs from 'node:fs';
import path from 'node:path';
import vm from 'node:vm';
import os from 'node:os';
import { execFileSync } from 'node:child_process';
const here=path.dirname(new URL(import.meta.url).pathname);
const hardenedDirectory=process.argv[2]||'hardened';
const read=(version,name)=>fs.readFileSync(path.join(here,version==='hardened'?hardenedDirectory:version,name),'utf8');
let checks=0;

async function errorWrites(version) {
  const writes=[];
  const context=vm.createContext({ServiceWorkerGlobalScope:{},Date,console:{info(){},error(){}},
    write:(key,value)=>writes.push({key,value})});
  const mod=new vm.SourceTextModule(read(version,'js/debug.js'),{context});
  await mod.link(async name=>{
    const code=name==='./ext-compat.js'
      ? 'export const dnr={}; export const normalizeDNRRules=x=>x; export const webext={runtime:{getManifest:()=>({permissions:[]})}};'
      : 'export const sessionRead=async()=>undefined; export const sessionWrite=(k,v)=>write(k,v);';
    const child=new vm.SourceTextModule(code,{context});await child.link(()=>{});return child;
  });
  await mod.evaluate();mod.namespace.ubolErr('private-url-canary');
  mod.namespace.toggleDeveloperMode(true);await Promise.resolve();
  return {writes,entries:mod.namespace.getConsoleOutput(),requests:await mod.namespace.getMatchedRules(1)};
}
const original=await errorWrites('upstream');
assert.ok(original.writes.some(x=>x.key==='console'));checks++;
const hardened=await errorWrites('hardened');
assert.equal(hardened.writes.length,0);assert.equal(hardened.entries.length,0);assert.equal(hardened.requests.length,0);checks++;

async function selectorRun(version) {
  const writes=[],inserts=[];
  const data={hostnames:['test.example'],regexes:[],selectorLists:['0'],selectors:['.advertisement'],selectorListRefs:[0],hasEntities:false};
  const self={specificImports:['test'],isolatedAPI:{contexts:{topHostname:'test.example',hostnames:['test.example'],entities:[]},binarySearch:()=>0},cssAPI:{insert:s=>inserts.push(s)}};
  const chrome={storage:{session:{get:async()=>({}),set:v=>writes.push(v)},local:{get:async key=>({[key]:key==='filteringModeDetails'?{none:[]}:structuredClone(data)})}}};
  const context={self,chrome,document:{location:{hostname:'test.example'}},Date};
  const code=read(version,'js/scripting/css-specific.js').replace('(async function uBOL_cssSpecific()', 'globalThis.finished = (async function uBOL_cssSpecific()');
  vm.runInNewContext(code,context);await context.finished;
  return {writes,inserts};
}
const oldCss=await selectorRun('upstream');assert.ok(oldCss.writes.some(x=>Object.keys(x).includes('cache.css.test.example')));checks++;
const newCss=await selectorRun('hardened');assert.equal(newCss.writes.length,0);assert.deepEqual(newCss.inserts,oldCss.inserts);assert.equal(newCss.inserts.length,1);checks++;

let network=0;
const ctx=vm.createContext({fetch:()=>{network++;throw Error('NETWORK_CALLED');}});
const imported=new vm.SourceTextModule(read('hardened','js/offscreen/fetch-list.js'),{context:ctx});
await imported.link(()=>{throw Error('UNEXPECTED_IMPORT');});await imported.evaluate();
await imported.namespace.fetchList({}, {urls:['https://example.invalid/private']},()=>{});
assert.equal(network,0);checks++;

const tmp=fs.mkdtempSync(path.join(os.tmpdir(),'r2d2-console-transform-'));
try {
  const code='globalThis.literal="console.log(secret)"; globalThis.calls=0; console.log(++globalThis.calls); const f=console.error.bind(console); f("secret");';
  fs.writeFileSync(path.join(tmp,'test.js'),code);
  execFileSync(process.execPath,[path.join(here,'strip-console.mjs'),tmp]);
  let calls=0;const context={console:{log:()=>calls++,error:()=>calls++}};
  vm.runInNewContext(fs.readFileSync(path.join(tmp,'test.js'),'utf8'),context);
  assert.equal(calls,0);assert.equal(context.calls,1);assert.equal(context.literal,'console.log(secret)');checks++;
} finally {fs.rmSync(tmp,{recursive:true,force:true});}

const manifest=JSON.parse(read('hardened','manifest.json'));
assert.ok(!manifest.permissions.includes('userScripts'));
assert.ok(!manifest.permissions.includes('declarativeNetRequestFeedback'));
assert.ok(manifest.content_security_policy.extension_pages.includes("connect-src 'self'"));checks++;
const before=JSON.parse(read('upstream','manifest.json'));
assert.deepEqual(manifest.declarative_net_request,before.declarative_net_request);checks++;
process.stdout.write(JSON.stringify({checksPassed:checks,originalLoggingDetected:true,originalHostCacheDetected:true,
  noLogAdapterVerified:true,cosmeticFilteringPreservedInFixture:true,nativeChromiumVerified:false})+'\n');
