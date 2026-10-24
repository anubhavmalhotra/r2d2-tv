#!/usr/bin/env python3
"""Render a configured, private build. No network, installer or TV commands."""
import argparse
import base64
import hashlib
import ipaddress
import json
import os
from pathlib import Path
import re
import stat
import sys
from ipk import archive, ar_member

ROOT = Path(__file__).resolve().parents[1]
PROFILE = 'webos-tv'


def need(ok, code):
    if not ok:
        raise ValueError(code)


def pairs(items):
    result = {}
    for key, value in items:
        need(key not in result, 'DUPLICATE_CONFIG_FIELD')
        result[key] = value
    return result


def config(raw):
    need(type(raw) is bytes and len(raw) <= 8192, 'CONFIG_SIZE')
    value = json.loads(raw, object_pairs_hook=pairs)
    need(type(value) is dict and set(value) == {
        'profile', 'spark_ip', 'tv_ip', 'spark_user', 'spark_hostname', 'spark_uid', 'weather'}, 'CONFIG_FIELDS')
    need(value['profile'] == PROFILE, 'UNREVIEWED_PROFILE')
    for key in ('spark_ip', 'tv_ip'):
        need(type(value[key]) is str, 'IP_TYPE')
        ip = ipaddress.IPv4Address(value[key])
        need(str(ip) == value[key] and not (ip.is_loopback or ip.is_multicast or ip.is_unspecified or ip.is_link_local), 'LAN_IPV4_REQUIRED')
        ranges = ('10.0.0.0/8','172.16.0.0/12','192.168.0.0/16','192.0.2.0/24')
        need(any(ip in ipaddress.IPv4Network(network) for network in ranges), 'LAN_IPV4_REQUIRED')
    need(value['spark_ip'] != value['tv_ip'], 'DISTINCT_HOSTS')
    need(type(value['spark_user']) is str and re.fullmatch(r'[a-z][a-z0-9_-]{0,30}', value['spark_user']), 'USER_NAME')
    need(value['spark_user'] != 'root', 'NONROOT_USER_REQUIRED')
    need(type(value['spark_hostname']) is str and re.fullmatch(r'[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?', value['spark_hostname']), 'HOST_NAME')
    # The native ownership and snap checks have only been qualified for uid1000.
    need(type(value['spark_uid']) is int and value['spark_uid'] == 1000, 'UNQUALIFIED_UID')
    # Weather has no location settings: Spark derives it at runtime from its public IP.
    weather = value['weather']
    need(type(weather) is dict and set(weather) == {'enabled'} and type(weather['enabled']) is bool, 'WEATHER_FIELDS')
    return value


def render(raw, cfg):
    substitutions = {
        'SPARK_IP': cfg['spark_ip'], 'TV_IP': cfg['tv_ip'],
        'SPARK_IP_REGEX': re.escape(cfg['spark_ip']),
        'SPARK_USER': cfg['spark_user'], 'SPARK_HOSTNAME': cfg['spark_hostname'],
        'WEATHER_ENABLED_PY': repr(cfg['weather']['enabled'])}
    def replace(match):
        need(match[1] in substitutions, 'UNKNOWN_TEMPLATE_FIELD')
        return substitutions[match[1]]
    result = re.sub(r'\{\{([A-Z_]+)\}\}', replace, raw.decode())
    need('{{' not in result, 'UNRESOLVED_TEMPLATE')
    return result.encode()


def save(path, raw):
    path.parent.mkdir(parents=True, mode=0o700, exist_ok=True)
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
    with os.fdopen(fd, 'wb') as stream:
        stream.write(raw)


def read_key(path):
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    with os.fdopen(fd, 'rb') as stream:
        info = os.fstat(stream.fileno())
        need(stat.S_ISREG(info.st_mode) and info.st_uid == os.getuid() and info.st_nlink == 1 and not info.st_mode & 0o077 and info.st_size <= 65, 'KEY_FILE_PERMISSIONS')
        raw = stream.read(66).strip()
    need(re.fullmatch(b'[a-f0-9]{64}', raw) is not None, 'KEY_FORMAT')
    return raw.decode()


# Optional private Home logos: {favorite app id: data URI}. Brand logos never ship
# in source; without this input each favorite renders its text mark.
LOGO_FILE_LIMIT = 1 << 20
LOGO_LIMIT = 64 << 10
SVG_DENIED = re.compile(rb'(?i)<script|<foreignobject|<!|href|\bon[a-z]+\s*=|url\(\s*[^#\s]')


def catalog_ids():
    raw = (ROOT/'tv/home/catalog.js.in').read_text()
    return {item['id'] for item in json.loads(raw.partition('window.OwnerCatalog = ')[2].rstrip().removesuffix(';'))['favorites']}


def home_logos(raw):
    need(type(raw) is bytes and len(raw) <= LOGO_FILE_LIMIT, 'LOGO_FILE_SIZE')
    value = json.loads(raw, object_pairs_hook=pairs)
    need(type(value) is dict and value and set(value) <= catalog_ids(), 'LOGO_APP_ID')
    for uri in value.values():
        need(type(uri) is str and re.fullmatch(r'data:image/(?:png|svg\+xml);base64,(?:[A-Za-z0-9+/]{4})*(?:[A-Za-z0-9+/]{2}==|[A-Za-z0-9+/]{3}=)?', uri) is not None, 'LOGO_FORMAT')
        kind, body = uri.split(',', 1)
        data = base64.b64decode(body, validate=True)
        need(len(data) <= LOGO_LIMIT, 'LOGO_SIZE')
        if kind == 'data:image/png;base64':
            need(data.startswith(b'\x89PNG\r\n\x1a\n\0\0\0\x0dIHDR') and data.endswith(b'IEND\xaeB`\x82'), 'LOGO_FORMAT')
        else:
            # Static vector only: no script, external reference, entity or handler.
            need(re.fullmatch(r'\s*(?:<\?xml[^>]*\?>\s*)?<svg[\s>][\s\S]*</svg>\s*', data.decode()) is not None and SVG_DENIED.search(data) is None, 'LOGO_FORMAT')
    return value


def read_logos(path):
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    with os.fdopen(fd, 'rb') as stream:
        info = os.fstat(stream.fileno())
        need(stat.S_ISREG(info.st_mode) and info.st_uid == os.getuid() and info.st_nlink == 1 and not info.st_mode & 0o077 and info.st_size <= LOGO_FILE_LIMIT, 'LOGO_FILE_PERMISSIONS')
        raw = stream.read(LOGO_FILE_LIMIT + 1)
    return home_logos(raw)


def package(home):
    info = json.loads((home/'appinfo.json').read_bytes())
    appid, version = info['id'], info['version']
    names = {'appinfo.json','index.html','style.css','catalog.js','core.js','app.js','icon.png','large-icon.png','LICENSE.md','THIRD_PARTY_NOTICES.md'}
    need({p.name for p in home.iterdir()} == names, 'HOME_FILE_SET')
    files = {f'usr/palm/applications/{appid}/{name}': (home/name).read_bytes() for name in sorted(names)}
    files[f'usr/palm/packages/{appid}/packageinfo.json'] = (json.dumps({'id':appid,'version':version,'app':appid}, sort_keys=True)+'\n').encode()
    control = (f'Package: {appid}\nVersion: {version}\nSection: misc\nPriority: optional\nArchitecture: all\n'
        'Maintainer: R2D2 contributors\nDescription: R2D2 private home\n'
        'webOS-Package-Format-Version: 2\n').encode()
    raw = b'!<arch>\n'+ar_member('debian-binary',b'2.0\n')+ar_member('control.tar.gz',archive({'control':control}))+ar_member('data.tar.gz',archive(files))
    return f'{appid}_{version}_all.ipk', raw


def build(cfg, out, key=None, include_tests=False, logos=None):
    need(not out.exists() and not out.is_symlink(), 'OUTPUT_EXISTS')
    need(out.parent.resolve() == out.parent.absolute(), 'OUTPUT_PARENT_SYMLINK')
    if logos is not None:
        logos = home_logos(json.dumps(logos).encode())
    out.mkdir(mode=0o700, parents=True)
    # Keep licensing with each deployable component, including the Home IPK.
    # Third-party declarations remain in place; the root MIT grant is scoped by
    # the accompanying notices and does not replace existing GPL declarations.
    for component in ('', 'home', 'spark', 'tv-resident'):
        for name in ('LICENSE.md', 'THIRD_PARTY_NOTICES.md'):
            save(out/component/name, (ROOT/name).read_bytes())
    for source, target in [('tv/home','home'),('spark','spark')]:
        for path in sorted((ROOT/source).rglob('*')):
            if not path.is_file() or path.is_symlink():
                continue
            relative = path.relative_to(ROOT/source)
            if 'tests' in relative.parts:
                continue
            need(path.suffix in ('.in','.png'), 'SOURCE_KIND')
            destination = out/target/relative
            raw = path.read_bytes()
            if path.suffix == '.in':
                destination = destination.with_suffix('')
                raw = render(raw, cfg)
            save(destination, raw)
    # The inherited-descriptor control channel and voice runtime pool/owner are
    # bundled into spark/voice_control.
    # Include the worker's bootstrap/provider/cleanup closure, not only the
    # parent-facing classes. Keep tests and standalone activation entries out.
    voice_files = tuple(name + '.py' for name in (
        'agent_controller', 'browser_channel', 'browser_spawn', 'channel_adapter',
        'child_probe', 'controller', 'cpu_sysfs_roles', 'descriptor_lineage',
        'diagnostic_inventory', 'execution_authority', 'filesystem_seal',
        'gpu_bootstrap_guard', 'gpu_bootstrap_readback', 'gpu_bootstrap_tsync',
        'ingress', 'lineage_adapter', 'local_qwen', 'lock_seal', 'memory_status',
        'model_adapter', 'model_assets', 'model_descriptor_roles', 'model_worker',
        'native_operations', 'pinned_models', 'probe_contract', 'process_isolation',
        'providers', 'resident_entry', 'resident_manager', 'resident_utterance', 'role_guard', 'role_operations', 'runtime_cleanup',
        'runtime_preload', 'sealed_startup', 'service', 'shared_owner',
        'socket_scope', 'startup_diagnostic', 'trial_control', 'tv_transport', 'ui_capture_broker', 'viewer_handoff',
        'voice_card_push', 'voice_copy', 'voice_status', 'worker_child', 'worker_runtime'))
    runtime_files = (
        ('voice/runtime/pool.py', 'pool.py'),
        ('voice/runtime/result.py', 'result.py'),
        ('voice/agent/nightly_maintenance.py', 'nightly_maintenance.py'),
        ('voice/agent/prototype/agent/warm.py', 'warm.py'),
        ('voice/agent/prototype/agent/errors.py', 'errors.py'),
    )
    save(out/'spark/voice_control/__init__.py', b'"""Owned browser voice control; inert on import."""\n')
    for name in voice_files:
        source = ROOT/'voice/backend'/name
        need(source.is_file() and not source.is_symlink(), 'VOICE_CONTROL_SOURCE')
        save(out/'spark/voice_control'/name, source.read_bytes())
    for rel_source, name in runtime_files:
        source = ROOT/rel_source
        need(source.is_file() and not source.is_symlink(), 'VOICE_CONTROL_SOURCE')
        save(out/'spark/voice_control'/name, source.read_bytes())
    for name in ('__init__.py', 'ram_credentials.py'):
        source = ROOT/'voice/backend/physical_capture'/name
        save(out/'spark/voice_control/physical_capture'/name, source.read_bytes())
    # AgentController is optional, but its packaged implementation must not
    # depend on a checkout elsewhere on the deployment host.
    agent_files = (
        '__init__.py', 'agent.py', 'audit.py', 'confirm.py', 'errors.py',
        'fastpath.py', 'killswitch.py', 'models.py', 'parse.py', 'policy.py',
        'registry.py', 'router.py', 'validate.py', 'sites.json',
        'executors/__init__.py', 'executors/base.py', 'executors/browser.py',
        'executors/feedback.py', 'executors/tv_gateway.py')
    for name in agent_files:
        source = ROOT/'voice/agent/prototype/agent'/name
        need(source.is_file() and not source.is_symlink(), 'VOICE_CONTROL_SOURCE')
        save(out/'spark/voice_control/agent'/name, source.read_bytes())
    # Existing TV enrollment and root route installation remain private platform
    # prerequisites. Ship the complete generic resident JS closure and decoder
    # sources; never copy a TV identity/profile or silently compile a binary.
    tv_resident = {
        'resident_main.js': 'main.js', 'resident_mic.js': 'resident_mic.js',
        'resident_hid.js': 'resident_hid.js', 'device_identity.js': 'device_identity.js',
        'owner_key.js': 'owner_key.js',
        'transport/tls_pcm_client.js': 'tls_pcm_client.js',
    }
    for source, destination in tv_resident.items():
        save(out/'tv-resident'/destination, (ROOT/'voice/tv'/source).read_bytes())
    for source in sorted((ROOT/'voice/tv/decoder').iterdir()):
        if source.is_file() and source.name in ('capture_core.c', 'capture_core.h', 'decode_reports.c', 'Makefile', 'README.md'):
            save(out/'tv-resident/decoder'/source.name, source.read_bytes())
    # Alternative LG capture reuse is packaged explicitly, not enabled by the
    # raw-HID main entry. No native binaries, migration record or identity ships.
    local_capture = (
        'main.js', 'bootstrap_entry.js', 'bus_context.js', 'power_reader.js', 'package.json', 'controller.js', 'service_port.js', 'owner_service.js',
        'session.js', 'legacy_provider.js', 'supervised_provider.js', 'tv_maintenance.js',
        'ui_capture.js', 'ui_heartbeat.js', 'ui_qualification.js', 'ui_authority.hpp', 'voice_feedback.js',
        'quiescence.js', 'patch_keyfilter.py', 'build_native.py',
        'native_handoff.cpp', 'native_handoff.hpp', 'ble_callback.hpp', 'native_retirement.hpp',
        'owned_provider.cpp', 'provider_supervisor.c', 'supervised_policy.h',
        'owner_retirement.h', 'startup_lock.hpp', 'README.md', 'NATIVE.md', 'QUIESCENCE.md',
    )
    for name in local_capture:
        source = ROOT/'voice/tv/local-capture'/name
        need(source.is_file() and not source.is_symlink(), 'LOCAL_CAPTURE_SOURCE')
        save(out/'tv-resident/local-capture'/name, source.read_bytes())
    for name in ('tls_pcm_client.js', 'pcm_provider_adapter.js'):
        save(out/'tv-resident/transport'/name, (ROOT/'voice/tv/transport'/name).read_bytes())
    save(out/'tv-resident/patch_firmware.py', (ROOT/'voice/tv/patch_firmware.py').read_bytes())
    if key is not None:
        need(re.fullmatch('[a-f0-9]{64}',key) is not None, 'KEY_FORMAT')
        catalog = out/'home/catalog.js'
        raw = catalog.read_text()
        need(raw.count('"key":null') == 1, 'CATALOG_KEY_SLOT')
        catalog.write_text(raw.replace('"key":null','"key":'+json.dumps(key)))
    if logos is not None:
        catalog = out/'home/catalog.js'
        raw = catalog.read_text()
        need(raw.count('"logos":{}') == 1, 'CATALOG_LOGO_SLOT')
        catalog.write_text(raw.replace('"logos":{}','"logos":'+json.dumps(logos,sort_keys=True)))
    if include_tests:
        for path in sorted((ROOT/'spark/tests').glob('*.in')):
            save(out/'spark/runtime'/path.with_suffix('').name, render(path.read_bytes(),cfg))
    name, raw = package(out/'home')
    save(out/name, raw)
    manifest = {'schema':1,'profile':cfg['profile'],'unsigned':True,'installed':False,
        'contains_private_configuration':True,'contains_browser_capability':key is not None,
        'contains_home_logos':logos is not None,
        'runtime_validation':'pending-on-target','files':{
        str(p.relative_to(out)):hashlib.sha256(p.read_bytes()).hexdigest()
        for p in sorted(out.rglob('*')) if p.is_file()}}
    save(out/'manifest.json',(json.dumps(manifest,indent=2,sort_keys=True)+'\n').encode())
    return manifest


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--config',type=Path,required=True)
    parser.add_argument('--output',type=Path,default=ROOT/'dist')
    parser.add_argument('--browser-key-file',type=Path)
    parser.add_argument('--home-logos-file',type=Path)
    parser.add_argument('--include-tests',action='store_true')
    args=parser.parse_args()
    try:
        cfg=config(args.config.read_bytes())
        key=read_key(args.browser_key_file) if args.browser_key_file else None
        logos=read_logos(args.home_logos_file) if args.home_logos_file else None
        result=build(cfg,args.output.absolute(),key,args.include_tests,logos)
    except (ValueError,OSError,UnicodeError):
        print('Build refused: inspect configuration, permissions and output path.',file=sys.stderr)
        return 1
    print(json.dumps({'built':True,'files':len(result['files']),'unsigned':True,'installed':False,'private_output':True}))
    return 0


if __name__=='__main__':
    raise SystemExit(main())
