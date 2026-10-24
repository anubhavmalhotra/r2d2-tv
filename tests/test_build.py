import base64
import copy
import hashlib
import importlib.util
import io
import json
import os
from pathlib import Path
import struct
import sys
import subprocess
import tempfile
import tarfile
import unittest
import zlib

ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT/'tools'))
import build
import privacy_check


def png(width=4,height=2):
    def chunk(kind,data):return struct.pack('>I',len(data))+kind+data+struct.pack('>I',zlib.crc32(kind+data))
    rows=b''.join(b'\0'+b'\xe3\xbd\x92\xff'*width for _ in range(height))
    return b'\x89PNG\r\n\x1a\n'+chunk(b'IHDR',struct.pack('>IIBBBBB',width,height,8,6,0,0,0))+chunk(b'IDAT',zlib.compress(rows))+chunk(b'IEND',b'')


def uri(kind,data):return 'data:image/'+kind+';base64,'+base64.b64encode(data).decode()


# Synthetic stand-ins only; real logos stay in the owner's private file.
SVG=b'<svg xmlns="http://www.w3.org/2000/svg" width="40" height="20"><path fill="#83ecff" d="M0 0h40v20H0Z"/></svg>'
LOGOS={'netflix':uri('png',png()),'hulu':uri('svg+xml',SVG)}


class BuildTests(unittest.TestCase):
    def setUp(self):self.cfg=build.config((ROOT/'config/example.json').read_bytes())

    def test_render_is_deterministic_and_package_is_data_only(self):
        with tempfile.TemporaryDirectory() as directory:
            left=Path(directory).resolve()/'first';right=Path(directory).resolve()/'second'
            a=build.build(self.cfg,left);b=build.build(self.cfg,right)
            self.assertEqual(a,b)
            package=next(left.glob('*.ipk')).read_bytes()
            self.assertTrue(package.startswith(b'!<arch>\n'))
            self.assertFalse(a['installed']);self.assertTrue(a['unsigned'])
            self.assertNotIn('{{',(left/'spark/runtime/viewer.js').read_text())
            self.assertIn('"key":null',(left/'home/catalog.js').read_text())
            self.assertFalse(a['contains_home_logos'])
            self.assertEqual((left/'home/catalog.js').read_text().count('"logos":{}'),1)
            self.assertEqual(left.stat().st_mode&0o777,0o700)
            self.assertTrue(all(p.stat().st_mode&0o777==0o600 for p in left.rglob('*') if p.is_file()))
            for component in ('', 'home', 'spark', 'tv-resident'):
                for notice in ('LICENSE.md', 'THIRD_PARTY_NOTICES.md'):
                    self.assertEqual((left/component/notice).read_bytes(), (ROOT/notice).read_bytes())
            offset=8; archived_notices={}
            while offset<len(package):
                header=package[offset:offset+60];size=int(header[48:58]);offset+=60
                if header[:16].decode().strip().rstrip('/')=='data.tar.gz':
                    with tarfile.open(fileobj=io.BytesIO(package[offset:offset+size]),mode='r:gz') as archive:
                        for notice in ('LICENSE.md','THIRD_PARTY_NOTICES.md'):
                            member=archive.getmember('./usr/palm/applications/local.owner.applibrary/'+notice)
                            archived_notices[notice]=archive.extractfile(member).read()
                offset+=size+(size%2)
            self.assertEqual(archived_notices,{name:(ROOT/name).read_bytes() for name in ('LICENSE.md','THIRD_PARTY_NOTICES.md')})
            for shipped in ('pool.py','result.py','nightly_maintenance.py','warm.py','shared_owner.py','service.py','model_worker.py','pinned_models.py','model_assets.py','resident_utterance.py','resident_entry.py','ui_capture_broker.py','resident_manager.py','tv_transport.py','voice_status.py','voice_copy.py','voice_card_push.py','physical_capture/ram_credentials.py'):
                self.assertTrue((left/'spark/voice_control'/shipped).is_file(),shipped)
                self.assertIn('spark/voice_control/'+shipped,a['files'])
            for shipped in ('main.js','resident_mic.js','resident_hid.js','owner_key.js','device_identity.js','tls_pcm_client.js','decoder/decode_reports.c'):
                self.assertIn('tv-resident/'+shipped,a['files'])
            self.assertFalse((left/'tv-resident/resident_config.json').exists())
            self.assertFalse((left/'tv-resident/decode-reports').exists())
            for shipped in ('local-capture/main.js','local-capture/bootstrap_entry.js','local-capture/bus_context.js','local-capture/power_reader.js','local-capture/package.json','local-capture/controller.js',
                            'local-capture/owner_service.js','local-capture/quiescence.js','local-capture/tv_maintenance.js',
                            'local-capture/ui_capture.js','local-capture/ui_heartbeat.js','local-capture/ui_qualification.js','local-capture/ui_authority.hpp','local-capture/voice_feedback.js',
                            'local-capture/owned_provider.cpp','local-capture/startup_lock.hpp','local-capture/build_native.py',
                            'transport/pcm_provider_adapter.js','patch_firmware.py'):
                self.assertIn('tv-resident/'+shipped,a['files'])
            self.assertFalse(list((left/'tv-resident').rglob('*.so')))
            self.assertFalse((left/'tv-resident/local-capture_config.json').exists())

    def test_local_capture_imports_from_distribution_without_target_actions(self):
        # Actual packaged dependencies must resolve without the source checkout,
        # palmbus, a compiler, enrolled credentials, processes or network calls.
        node = __import__('shutil').which('node')
        self.assertIsNotNone(node)
        script = r'''
const Module=require('module'),path=require('path');
const root=path.resolve(process.argv[1]), load=Module._load;
const denied=new Set(['palmbus','/usr/lib/nodejs/palmbus']);
Module._load=function(name,parent,isMain){
  if(denied.has(name))throw Error('TARGET_BUS_IMPORT');
  const value=load.call(this,name,parent,isMain);
  if(name==='child_process')for(const k of ['spawn','exec','execFile','fork','spawnSync','execFileSync'])value[k]=()=>{throw Error('CHILD_FORBIDDEN');};
  if(name==='net')for(const k of ['connect','createConnection','createServer'])value[k]=()=>{throw Error('NETWORK_FORBIDDEN');};
  if(name==='tls')for(const k of ['connect','createServer'])value[k]=()=>{throw Error('NETWORK_FORBIDDEN');};
  if(name==='http')for(const k of ['request','get','createServer'])value[k]=()=>{throw Error('NETWORK_FORBIDDEN');};
  return value;
};
for(const name of ['main','bootstrap_entry','bus_context','power_reader','controller','service_port','owner_service','session','legacy_provider','supervised_provider','tv_maintenance','quiescence','ui_capture','ui_heartbeat','ui_qualification','voice_feedback'])
  require(path.join(root,'local-capture',name+'.js'));
const entry=require(path.join(root,'local-capture'));
if(Object.keys(entry).length||'run' in entry)throw Error('BOOTSTRAP_ENTRY_EXPORT');
for(const name of Object.keys(require.cache))if(name.includes('local-capture')&&!name.startsWith(root+path.sep))throw Error('CHECKOUT_IMPORT');
console.log(JSON.stringify({standalone:true,deviceStarted:false}));
'''
        with tempfile.TemporaryDirectory() as directory:
            out=Path(directory).resolve()/'build'
            build.build(self.cfg,out)
            result=subprocess.run([node,'-e',script,str(out/'tv-resident')],cwd=directory,
                                  capture_output=True,text=True,timeout=10)
            self.assertEqual(result.returncode,0,result.stderr)
            self.assertEqual(json.loads(result.stdout),{'standalone':True,'deviceStarted':False})

    def test_bad_configuration_cannot_inject_executable_source(self):
        for field,value in [('spark_user',"x';boom()#"),('spark_hostname','x;id'),('spark_ip','127.0.0.1'),('spark_ip','8.8.8.8'),('tv_ip',self.cfg['spark_ip']),('spark_uid',False),('spark_uid',0),('profile','other')]:
            cfg=copy.deepcopy(self.cfg);cfg[field]=value
            with self.assertRaises((ValueError,TypeError)):build.config(json.dumps(cfg).encode())

    def test_voice_owner_runs_from_rendered_package_without_source_tree(self):
        # A source-tree PYTHONPATH hides missing files and mixed flat/package
        # imports. Exercise the actual distribution in a clean interpreter.
        code = r'''
import importlib, json, pathlib, runpy, ssl, sys
root = pathlib.Path(sys.argv[1])
sys.path.insert(0, str(root / 'spark'))
def deny_external(event, args):
    if event in ('socket.__new__', 'socket.connect', 'socket.bind',
                 'subprocess.Popen', 'os.system', 'os.exec', 'os.posix_spawn',
                 'os.kill', 'os.killpg'):
        raise AssertionError('EXTERNAL_ACTION_FORBIDDEN')
    if event == 'import' and args[0].split('.')[0] in ('torch', 'transformers', 'numpy'):
        raise AssertionError('MODEL_IMPORT_FORBIDDEN')
sys.addaudithook(deny_external)
import manager
from voice_control import pool
from voice_control.shared_owner import SharedVoiceOwner
from voice_control.viewer_handoff import TVViewerHandoff
from voice_control.controller import Refused
from voice_control.model_worker import Refused as WorkerRefused
assert WorkerRefused is Refused
for path in (root / 'spark/voice_control').glob('*.py'):
    importlib.import_module('voice_control.' + path.stem)
runpy.run_path(str(root / 'spark/voice_control/worker_child.py'), run_name='inert_child')
def forbidden(*args, **kwargs):
    raise AssertionError('NATIVE_ACTION_FORBIDDEN')
for agent_mode in (False, True):
    manager_instance = manager.Manager(spawn=forbidden)
    workers = pool.create_production_pool('/usr/bin/python3', '/synthetic/asr', '/synthetic/intent')
    worker = workers.factory()
    assert worker.process is None and worker.runtime is None
    assert worker.close()
    viewer = TVViewerHandoff(manager.SPARK, 'c' * 64, forbidden, qualified=True)
    context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    context.minimum_version = context.maximum_version = ssl.TLSVersion.TLSv1_3
    context.verify_mode = ssl.CERT_REQUIRED
    owner = SharedVoiceOwner(manager_instance, workers, viewer, 'c' * 64,
                             manager.SPARK, 4443, context, 'a' * 64, b'k' * 32,
                             enabled=True, agent_mode=agent_mode)
    assert owner.state == 'new' and owner.service.listener is None
    assert owner.close() and owner.cleanup_complete
    manager_instance.close()
for name, module in tuple(sys.modules.items()):
    if name == 'manager' or name.startswith('voice_control.'):
        assert pathlib.Path(module.__file__).resolve().is_relative_to(root)
assert 'controller' not in sys.modules
assert 'local_qwen' not in sys.modules
print(json.dumps({'standalone': True, 'modelsLoaded': False, 'listenerOpened': False}))
'''
        with tempfile.TemporaryDirectory() as directory:
            out=Path(directory).resolve()/'build'
            build.build(self.cfg,out)
            result=subprocess.run([sys.executable,'-I','-B','-c',code,str(out)],
                                  cwd=directory,capture_output=True,text=True,timeout=10)
            self.assertEqual(result.returncode,0,result.stderr)
            self.assertEqual(json.loads(result.stdout),
                             {'standalone':True,'modelsLoaded':False,'listenerOpened':False})

    def test_duplicate_fields_rejected(self):
        with self.assertRaises(ValueError):build.config(b'{"profile":1,"profile":2}')

    def test_special_ipv4_ranges_rejected(self):
        for address in ('240.1.2.3','0.1.2.3','255.255.255.255','198.18.0.1','100.64.0.1'):
            cfg=copy.deepcopy(self.cfg);cfg['spark_ip']=address
            with self.assertRaises(ValueError):build.config(json.dumps(cfg).encode())

    def test_weather_config_is_only_an_enabled_flag(self):
        self.assertEqual(json.loads((ROOT/'config/example.json').read_bytes())['weather'],{'enabled':False})
        for enabled in (True,False):
            cfg=copy.deepcopy(self.cfg);cfg['weather']={'enabled':enabled}
            self.assertEqual(build.config(json.dumps(cfg).encode()),cfg)
        # No location of any kind is configurable: Spark derives it from its public IP.
        rejected=[{'enabled':True,'zip':''},{'enabled':False,'zip':''},{'enabled':True,'station':'KAAA'},
                  {'enabled':True,'fallbackStations':[]},{'enabled':True,'fallbackStations':['KBBB']},
                  {'enabled':True,'zip':'','station':'','fallbackStations':[]},{'enabled':True,'lat':40.0,'lon':-100.0},
                  {},{'enabled':1},{'enabled':'true'},{'enabled':None},[],None,'on',{'enabled':True,'extra':1}]
        for weather in rejected:
            cfg=copy.deepcopy(self.cfg);cfg['weather']=weather
            with self.assertRaisesRegex(ValueError,'^WEATHER_FIELDS$',msg=weather):build.config(json.dumps(cfg).encode())
        raw=json.dumps(self.cfg).replace('"enabled": false','"enabled": false, "enabled": true').encode()
        self.assertEqual(raw.count(b'"enabled"'),2)
        with self.assertRaises(ValueError):build.config(raw)

    def test_weather_renders_without_any_location_template(self):
        for name in ('spark/weather.py.in','tv/home/core.js.in','tv/home/app.js.in','tv/home/index.html.in'):
            source=(ROOT/name).read_text()
            for field in ('WEATHER_ZIP','WEATHER_STATION','WEATHER_FALLBACK_STATIONS_PY'):
                self.assertNotIn(field,source,name)
        for field in ('{{WEATHER_ZIP}}','{{WEATHER_STATION}}','{{WEATHER_FALLBACK_STATIONS_PY}}'):
            with self.assertRaisesRegex(ValueError,'UNKNOWN_TEMPLATE_FIELD'):build.render(field.encode(),self.cfg)
        source=(ROOT/'spark/weather.py.in').read_bytes()
        for enabled in (True,False):
            cfg=copy.deepcopy(self.cfg);cfg['weather']={'enabled':enabled}
            rendered=build.render(source,build.config(json.dumps(cfg).encode())).decode()
            self.assertIn('ENABLED = '+repr(enabled),rendered.splitlines())
            namespace={'__name__':'rendered_weather'}
            exec(compile(rendered,'<rendered weather>','exec'),namespace)
            self.assertFalse({'ZIP','STATION','FALLBACK_STATIONS'}&set(namespace))
            self.assertEqual(namespace['unavailable']()['zip'],'')
            self.assertEqual(namespace['metar_path'](('KAAA','KBBB')),'/api/data/metar?ids=KAAA,KBBB&format=json')

    def test_unknown_template_and_existing_output_refused(self):
        with self.assertRaises(ValueError):build.render(b'{{UNKNOWN}}',self.cfg)
        with tempfile.TemporaryDirectory() as directory:
            with self.assertRaises(ValueError):build.build(self.cfg,Path(directory))

    def test_source_ships_no_logo_payloads(self):
        for path in sorted((ROOT/'tv/home').iterdir()):
            if path.suffix=='.in':
                self.assertNotIn(b'data:image',path.read_bytes(),path.name);self.assertNotIn(b'base64',path.read_bytes(),path.name)
        raw=(ROOT/'tv/home/catalog.js.in').read_text()
        self.assertEqual(raw.count('"logos":{}'),1)
        catalog=json.loads(raw.partition('window.OwnerCatalog = ')[2].rstrip().removesuffix(';'))
        self.assertEqual(catalog['logos'],{})
        self.assertTrue(all(set(item)=={'id','title','mark','tone','headline','description','tag'} for item in catalog['favorites']))
        self.assertEqual(build.catalog_ids(),{item['id'] for item in catalog['favorites']})
        self.assertEqual(len(build.catalog_ids()),10)

    def test_home_logos_enter_only_through_private_file(self):
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory).resolve();source=root/'logos.json'
            fd=os.open(source,os.O_WRONLY|os.O_CREAT|os.O_EXCL,0o600)
            with os.fdopen(fd,'w') as stream:json.dump(LOGOS,stream)
            logos=build.read_logos(source)
            self.assertEqual(logos,LOGOS)
            result=build.build(self.cfg,root/'build',logos=logos)
            self.assertTrue(result['contains_home_logos']);self.assertFalse(result['contains_browser_capability'])
            raw=(root/'build/home/catalog.js').read_text()
            catalog=json.loads(raw.partition('window.OwnerCatalog = ')[2].rstrip().removesuffix(';'))
            self.assertEqual(catalog['logos'],LOGOS);self.assertNotIn('"logos":{}',raw)
            self.assertEqual(catalog['browser']['key'],None)
            # The manifest records only that logos were supplied, never which or their digests.
            manifest=(root/'build/manifest.json').read_text()
            self.assertEqual(json.loads(manifest),result)
            for appid,value in LOGOS.items():
                for digest in (hashlib.sha256(value.encode()).hexdigest(),hashlib.sha256(base64.b64decode(value.split(',',1)[1])).hexdigest()):
                    self.assertNotIn(digest,manifest)
                self.assertNotIn(value.split(',',1)[1][:24],manifest)
            self.assertEqual(set(result)-{'files'},{'schema','profile','unsigned','installed','contains_private_configuration','contains_browser_capability','contains_home_logos','runtime_validation'})
            keyed=build.build(self.cfg,root/'keyed',key='b'*64,logos=logos)
            raw=(root/'keyed/home/catalog.js').read_text()
            self.assertIn('"key":"'+'b'*64+'"',raw);self.assertEqual(json.loads(raw.partition('window.OwnerCatalog = ')[2].rstrip().removesuffix(';'))['logos'],LOGOS)
            self.assertTrue(keyed['contains_home_logos'] and keyed['contains_browser_capability'])
            # The CLI path: accepted when private, refused when readable by others.
            command=[sys.executable,'-B',str(ROOT/'tools/build.py'),'--config',str(ROOT/'config/example.json'),'--home-logos-file',str(source)]
            done=subprocess.run(command+['--output',str(root/'cli')],capture_output=True,text=True,timeout=60)
            self.assertEqual(done.returncode,0,done.stderr)
            self.assertTrue(json.loads((root/'cli/manifest.json').read_text())['contains_home_logos'])
            self.assertEqual((root/'cli/home/catalog.js').read_text(),(root/'build/home/catalog.js').read_text())
            os.chmod(source,0o644)
            refused=subprocess.run(command+['--output',str(root/'cli-open')],capture_output=True,text=True,timeout=60)
            self.assertEqual(refused.returncode,1);self.assertFalse((root/'cli-open').exists())
            self.assertNotIn(LOGOS['netflix'][-24:],refused.stdout+refused.stderr)

    def test_home_logo_file_permissions_refused(self):
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory).resolve();good=root/'logos.json'
            fd=os.open(good,os.O_WRONLY|os.O_CREAT|os.O_EXCL,0o600)
            with os.fdopen(fd,'w') as stream:json.dump(LOGOS,stream)
            self.assertEqual(build.read_logos(good),LOGOS)
            os.symlink(good,root/'link.json')
            with self.assertRaises(OSError):build.read_logos(root/'link.json')
            os.link(good,root/'hard.json')
            with self.assertRaisesRegex(ValueError,'^LOGO_FILE_PERMISSIONS$'):build.read_logos(good)
            os.unlink(root/'hard.json')
            for mode in (0o640,0o604,0o644):
                os.chmod(good,mode)
                with self.assertRaisesRegex(ValueError,'^LOGO_FILE_PERMISSIONS$'):build.read_logos(good)
            os.chmod(good,0o600)
            big=root/'big.json';fd=os.open(big,os.O_WRONLY|os.O_CREAT|os.O_EXCL,0o600)
            with os.fdopen(fd,'wb') as stream:stream.write(b' '*(build.LOGO_FILE_LIMIT+1))
            with self.assertRaisesRegex(ValueError,'^LOGO_FILE_PERMISSIONS$'):build.read_logos(big)
            os.mkdir(root/'dir',0o700)
            with self.assertRaises((ValueError,OSError)):build.read_logos(root/'dir')

    def test_home_logos_validated_strictly(self):
        good=LOGOS['netflix'];body=good.split(',',1)[1]
        def svg(markup):return uri('svg+xml',markup)
        bad_uris=[None,1,[],{},'',good.replace('png','jpeg',1),good.replace('png','gif',1),'data:image/png,'+body,
                  'DATA:image/png;base64,'+body,'data:text/html;base64,'+body,'data:image/png;charset=utf-8;base64,'+body,
                  'data:image/svg;base64,'+body,' '+good,good+' ',good+'\n',good[:-1],good[:-2]+'@=',
                  'data:image/png;base64,'+body[:40]+'\n'+body[40:],'data:image/png;base64,'+body.replace('+','-').replace('/','_')+'-_',
                  'data:image/png;base64,','data:image/png;base64,'+'='*4,
                  uri('png',SVG),uri('png',png()[:-12]),uri('png',png()[:8]),uri('png',png()+b'trailing'),uri('svg+xml',png()),
                  uri('png',b'\x89PNG\r\n\x1a\n\0\0\0\x0dIHDR'+b'\0'*(build.LOGO_LIMIT)+b'IEND\xaeB`\x82'),
                  svg(b'<svg xmlns="http://www.w3.org/2000/svg"><script>alert(1)</script></svg>'),
                  svg(b'<svg xmlns="http://www.w3.org/2000/svg" onload="alert(1)"></svg>'),
                  svg(b'<svg xmlns="http://www.w3.org/2000/svg"><image href="http://example.com/x.png"/></svg>'),
                  svg(b'<svg xmlns="http://www.w3.org/2000/svg" xmlns:xlink="http://www.w3.org/1999/xlink"><use xlink:href="#a"/></svg>'),
                  svg(b'<!DOCTYPE svg [<!ENTITY e "x">]><svg xmlns="http://www.w3.org/2000/svg">&e;</svg>'),
                  svg(b'<svg xmlns="http://www.w3.org/2000/svg"><!-- note --></svg>'),
                  svg(b'<svg xmlns="http://www.w3.org/2000/svg"><foreignObject/></svg>'),
                  svg(b'<svg xmlns="http://www.w3.org/2000/svg"><path style="fill:url(http://example.com/a)"/></svg>'),
                  svg(b'<html><svg xmlns="http://www.w3.org/2000/svg"/></html>'),svg(b'<svgx></svgx>'),svg(b'\xff\xfe<svg></svg>'),svg(b'')]
        for value in bad_uris:
            with self.assertRaises(ValueError,msg=repr(value)[:80]):build.home_logos(json.dumps({'netflix':value}).encode())
        for raw in [b'',b'{}',b'[]',b'null',b'"x"',json.dumps([good]).encode(),
                    json.dumps({'com.example.unknown':good}).encode(),json.dumps({'com.webos.app.hdmi1':good}).encode(),
                    json.dumps({'local.owner.applibrary':good}).encode(),json.dumps({'Netflix':good}).encode(),
                    ('{"netflix":"%s","netflix":"%s"}'%(good,good)).encode(),b'{"netflix":"'+good.encode()+b'"',
                    b' '*(build.LOGO_FILE_LIMIT+1),'{"netflix":"%s"}'.encode()]:
            with self.assertRaises(ValueError,msg=raw[:60]):build.home_logos(raw)
        with self.assertRaisesRegex(ValueError,'^LOGO_APP_ID$'):build.home_logos(json.dumps({'hulu':good,'com.example.unknown':good}).encode())
        with self.assertRaisesRegex(ValueError,'^LOGO_SIZE$'):build.home_logos(json.dumps({'netflix':uri('png',png(400,400)[:8]+b'\0'*(build.LOGO_LIMIT+1))}).encode())
        self.assertEqual(build.home_logos(json.dumps({'netflix':uri('svg+xml',b'<?xml version="1.0"?>\n'+SVG+b'\n')}).encode())['netflix'][:26],'data:image/svg+xml;base64,')
        # build() re-validates, and refuses before creating any output.
        with tempfile.TemporaryDirectory() as directory:
            out=Path(directory).resolve()/'build'
            for logos in ({'com.example.unknown':good},{'netflix':'data:image/jpeg;base64,'+body},{}):
                with self.assertRaises(ValueError):build.build(self.cfg,out,logos=logos)
                self.assertFalse(out.exists())

    def test_private_capability_only_enters_private_build(self):
        with tempfile.TemporaryDirectory() as directory:
            out=Path(directory).resolve()/'build'
            result=build.build(self.cfg,out,key='a'*64)
            self.assertTrue(result['contains_browser_capability'])
            self.assertIn('"key":"'+'a'*64+'"',(out/'home/catalog.js').read_text())
            self.assertNotIn('a'*64,(ROOT/'tv/home/catalog.js.in').read_text())


class PrivacyTests(unittest.TestCase):
    def test_generated_paths_and_secret_patterns(self):
        self.assertIn('private-or-generated-path',privacy_check.issues('private/credentials.json',b'{}'))
        self.assertIn('private-or-generated-path',privacy_check.issues('startup-candidates.private.json',b'{}'))
        self.assertIn('github-token',privacy_check.issues('x.py',('ghp_'+'a'*40).encode()))
        self.assertIn('personal-home-path',privacy_check.issues('x.py',('/Users/'+'person/project').encode()))
        self.assertIn('private-ipv4',privacy_check.issues('x.py',('.'.join(['192','168','5','23'])).encode()))

    def test_documentation_addresses_and_templates_allowed(self):
        self.assertEqual(privacy_check.issues('config/example.json',b'192.0.2.10'),[])
        self.assertEqual(privacy_check.issues('x.py.in',b'/home/{{SPARK_USER}}/runtime'),[])
        self.assertEqual(privacy_check.issues('x.py',b'tv/home/catalog.js.in'),[])

    def test_browser_capability_in_catalog_is_detected(self):
        raw=json.dumps({'browser':{'origin':'http://192.0.2.10:4181','key':'a1'*32}}).encode()
        self.assertIn('embedded-capability',privacy_check.issues('tv/home/catalog.js.in',raw))
        self.assertEqual(privacy_check.issues('tv/home/catalog.js.in',b'{"browser":{"key":null}}'),[])


if __name__=='__main__':unittest.main()
