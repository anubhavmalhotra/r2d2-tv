"""Build an explicitly modified, source-pinned uBOL candidate. No installation."""
import hashlib
import json
from pathlib import Path
import shutil
import subprocess
import sys

HERE=Path(__file__).resolve().parent
ZIP_SHA='73909c8f26d608b00a99f446192630868bea4f947ba293c29bbd18ae948d903b'
NODE=shutil.which('node')
if NODE is None: raise RuntimeError('NODE_REQUIRED')

def header(text):
    return text[:text.index('*/')+2]+'\n\n'

def build():
    assert hashlib.sha256((HERE/'upstream.zip').read_bytes()).hexdigest()==ZIP_SHA
    upstream=HERE/'upstream'
    tree=json.loads((HERE/'upstream-tree.json').read_text())
    source={x['path'][9:]:x['sha'] for x in tree['tree']
            if x['path'].startswith('chromium/') and x['type']=='blob'}
    original={p.relative_to(upstream).as_posix():p.read_bytes()
              for p in upstream.rglob('*') if p.is_file()}
    assert len(original)==930 and set(source)-set(original)=={'log.txt'}
    for name,data in original.items():
        assert hashlib.sha1(b'blob '+str(len(data)).encode()+b'\0'+data).hexdigest()==source[name],name
    output=HERE/(sys.argv[1] if len(sys.argv)==2 else 'hardened')
    if output.parent!=HERE or not output.name.startswith('hardened'):
        raise RuntimeError('OUTPUT_SCOPE')
    if output.exists():
        raise RuntimeError('OUTPUT_ALREADY_EXISTS')
    shutil.copytree(upstream,output)

    # Keep the public debug API, with no error ring, console output, matched-
    # request buffer, listeners, imports or storage reads/writes.
    p=output/'js/debug.js'
    p.write_text(header(p.read_text())+'''// R2D2 local patch: no extension diagnostic or matched-request logging.
export const isSideloaded = false;
export const ubolLog = () => {};
export const ubolErr = () => {};
export const getConsoleOutput = () => [];
export const getMatchedRules = async () => [];
export const toggleDeveloperMode = () => {};
''')

    # Compute selectors per page. Avoid storing visited hostnames and visit times
    # in extension storage; keep the bundled ruleset cache and filtering intact.
    p=output/'js/scripting/css-specific.js';text=p.read_text()
    start=text.index('const sessionRead = async function(key) {')
    end=text.index('const localRead = async function(key) {',start)
    text=text[:start]+text[end:]
    start=text.index('const cachePath = topHostname !== thisHostname')
    end=text.index('const { s, p } = cacheEntry;',start)
    text=text[:start]+'''// R2D2: selectors live only in this page's isolated execution context.
let cacheEntry = { s: [], p: [] };
cacheEntry = await fillCache(specificImports);

'''+text[end:]
    p.write_text(text)

    # A TV appliance uses reviewed bundled filters, not runtime list downloads.
    p=output/'js/offscreen/fetch-list.js'
    p.write_text(header(p.read_text())+'''// R2D2: external filter subscriptions are disabled in this build.
export async function fetchList() { return undefined; }
''')
    # Upstream's explicit issue-report action includes the selected URL/config.
    # Remove that optional sharing UI for this private appliance build.
    p=output/'js/report.js'
    p.write_text(header(p.read_text())+'''// R2D2: no diagnostic report or URL-sharing flow.
document.body.textContent = 'External reporting is disabled in this private browser.';
''')
    p=output/'js/popup.js';text=p.read_text()
    start=text.index("dom.on('#gotoReport', 'click', ev => {")
    end=text.index("dom.on('#gotoDashboard'",start)
    text=text[:start]+"dom.remove('#gotoReport');\n\n"+text[end:]
    p.write_text(text)
    p=output/'manifest.json';manifest=json.loads(p.read_text())
    manifest['name']='R2D2 Content Protection (uBO Lite derived)'
    manifest['short_name']='R2D2 Protection'
    manifest['description']='Locally hardened uBO Lite: bundled filtering, no request/error log, no external filter downloads.'
    manifest['permissions'].remove('userScripts')
    manifest['content_security_policy']={'extension_pages':
        "default-src 'self'; script-src 'self'; object-src 'none'; connect-src 'self'; "
        "img-src 'self' data:; style-src 'self' 'unsafe-inline'; base-uri 'none'; form-action 'none'"}
    p.write_text(json.dumps(manifest,indent=2)+'\n')

    result=subprocess.run([NODE,str(HERE/'strip-console.mjs'),str(output)],
                          check=True,text=True,capture_output=True)
    console_changes=json.loads(result.stdout)
    changed=[];pins={}
    for p in sorted(output.rglob('*')):
        if not p.is_file():continue
        name=p.relative_to(output).as_posix();data=p.read_bytes()
        pins[name]=hashlib.sha256(data).hexdigest()
        if data!=original[name]:changed.append(name)
    receipt={'upstreamRelease':'2026.1012.1545','upstreamZipSha256':ZIP_SHA,
             'sourceBlobMatches':930,'modifiedFiles':changed,'consoleChanges':console_changes,
             'filterDataUnchanged':all(not p.startswith('rulesets/main/') for p in changed),
             'installed':False,'nativeBrowserVerified':False,'backdoorAbsenceProven':False}
    receipt['output']=output.name
    (HERE/(output.name+'-pins.json')).write_text(json.dumps(pins,indent=2,sort_keys=True)+'\n')
    (HERE/(output.name+'-receipt.json')).write_text(json.dumps(receipt,indent=2)+'\n')
    print(json.dumps(receipt,indent=2))

if __name__=='__main__':build()
