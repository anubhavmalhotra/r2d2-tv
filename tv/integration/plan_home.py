"""Offline Home/idle JSON planner. No device access or deployment capability.

Inputs and generated files remain local/private. A reviewed fingerprint contract
is required; matching supplied hashes is not an independent firmware audit.
"""
import argparse
import copy
import hashlib
import json
import os
from pathlib import Path
import re
import stat
import sys

MAX_JSON = 262144
MAX_ARTIFACT = 64 * 1024 * 1024
ARTIFACT_ROLES = {'application_manager', 'system_profile', 'configuration_service',
                  'compositor', 'boot_sequencer'}
SHA = re.compile(r'[0-9a-f]{64}')
APP = re.compile(r'[a-z][a-z0-9]*(?:[.-][a-z0-9]+)+')

class Refusal(ValueError):
    pass

def require(ok, code):
    if not ok:
        raise Refusal(code)

def digest(raw):
    return hashlib.sha256(raw).hexdigest()

def pairs(items):
    result = {}
    for key, value in items:
        require(key not in result, 'DUPLICATE_JSON_KEY')
        result[key] = value
    return result

def decode(raw):
    require(type(raw) is bytes and len(raw) <= MAX_JSON, 'JSON_BOUND')
    def constant(_):
        raise Refusal('NONFINITE_JSON')
    try:
        result = json.loads(raw, object_pairs_hook=pairs, parse_constant=constant)
    except (UnicodeError, json.JSONDecodeError, RecursionError):
        raise Refusal('JSON_INVALID') from None
    def bounded(value, depth=0):
        require(depth <= 24, 'JSON_DEPTH')
        if type(value) is dict:
            require(len(value) <= 1024, 'OBJECT_BOUND')
            for key, child in value.items():
                require(type(key) is str and len(key) <= 256, 'KEY_BOUND')
                bounded(child, depth + 1)
        elif type(value) is list:
            require(len(value) <= 1024, 'ARRAY_BOUND')
            for child in value:
                bounded(child, depth + 1)
        elif type(value) is str:
            require(len(value) <= 8192, 'STRING_BOUND')
        elif type(value) in (int, float):
            require(abs(value) <= 2**53, 'NUMBER_BOUND')
        else:
            require(value is None or type(value) is bool, 'JSON_TYPE')
    bounded(result)
    return result

def encode(value):
    raw = (json.dumps(value, sort_keys=True, ensure_ascii=False, allow_nan=False,
                      indent=2) + '\n').encode()
    require(len(raw) <= MAX_JSON, 'OUTPUT_BOUND')
    return raw

def app_id(value, owner=False):
    require(type(value) is str and len(value) <= 128 and APP.fullmatch(value), 'APP_ID')
    if owner:
        require(not value.startswith(('com.webos.', 'com.lge.', 'com.palm.')), 'OWNER_NAMESPACE')
    return value

def exact(value, fields, code):
    require(type(value) is dict and set(value) == set(fields), code)

def file_hash(path, limit, keep=False):
    """Canonical regular file, bounded streaming and stable path/fd identity."""
    path = Path(path).absolute()
    require(path.resolve() == path, 'NONCANONICAL_INPUT')
    before = path.lstat()
    require(stat.S_ISREG(before.st_mode) and before.st_nlink == 1 and
            0 < before.st_size <= limit, 'INPUT_FILE')
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    ident = lambda s: (s.st_dev, s.st_ino, s.st_mode, s.st_uid, s.st_gid,
                      s.st_size, s.st_mtime_ns, s.st_ctime_ns, s.st_nlink)
    try:
        require(ident(os.fstat(fd)) == ident(before), 'INPUT_CHANGED')
        h = hashlib.sha256(); total = 0; chunks = []
        while True:
            block = os.read(fd, min(65536, limit + 1 - total))
            if not block:
                break
            total += len(block); require(total <= limit, 'INPUT_BOUND'); h.update(block)
            if keep:
                chunks.append(block)
        require(total == before.st_size and ident(os.fstat(fd)) == ident(before) and
                ident(path.lstat()) == ident(before) and path.resolve() == path,
                'INPUT_CHANGED')
        return h.hexdigest(), b''.join(chunks) if keep else None
    finally:
        os.close(fd)

def verify_contract(contract, original_root):
    exact(contract, ('schema', 'firmware', 'artifacts', 'snapshotSha256',
                     'expectedHome', 'expectedIdle'), 'CONTRACT_SCHEMA')
    require(type(contract['schema']) is int and contract['schema'] == 1, 'CONTRACT_VERSION')
    exact(contract['firmware'], ('release', 'build', 'architecture'), 'FIRMWARE_SCHEMA')
    require(all(type(v) is str and 0 < len(v) <= 128 and v.isascii()
                for v in contract['firmware'].values()), 'FIRMWARE_ID')
    require(type(contract['snapshotSha256']) is str and
            SHA.fullmatch(contract['snapshotSha256']), 'SNAPSHOT_DIGEST')
    app_id(contract['expectedHome']); app_id(contract['expectedIdle'])
    exact(contract['artifacts'], ARTIFACT_ROLES, 'ARTIFACT_ROLES')
    root = Path(original_root).absolute()
    require(root.resolve() == root and root.is_dir(), 'ORIGINAL_ROOT')
    verified = {}
    for role, item in contract['artifacts'].items():
        exact(item, ('path', 'sha256'), 'ARTIFACT_SCHEMA')
        name = item['path']
        require(type(name) is str and 0 < len(name) <= 256 and '\\' not in name,
                'ARTIFACT_PATH')
        relative = Path(name)
        require(not relative.is_absolute() and all(p not in ('.', '..') for p in name.split('/')),
                'ARTIFACT_PATH')
        require(type(item['sha256']) is str and SHA.fullmatch(item['sha256']), 'ARTIFACT_DIGEST')
        actual, _ = file_hash(root / relative, MAX_ARTIFACT)
        require(actual == item['sha256'], 'ARTIFACT_HASH')
        verified[role] = actual
    return verified

def candidates(snapshot, contract, owner):
    """Pure transformation; never reads or changes the caller's objects."""
    app_id(owner, owner=True)
    exact(snapshot, ('schema', 'firmware', 'profile', 'configIdleApp',
                     'configProfileList', 'lastAppHandlerPolicy', 'factoryIdleApp'),
          'SNAPSHOT_SCHEMA')
    require(type(snapshot['schema']) is int and snapshot['schema'] == 1, 'SNAPSHOT_VERSION')
    require(snapshot['firmware'] == contract['firmware'], 'FIRMWARE_MISMATCH')
    profile = snapshot['profile']
    require(type(profile) is dict and type(profile.get('defaultApps')) is dict and
            type(profile.get('idleApp')) is dict, 'PROFILE_SCHEMA')
    defaults, idle = profile['defaultApps'], profile['idleApp']
    require(type(defaults.get('categories')) is dict and
            defaults['categories'].get('home') == contract['expectedHome'], 'HOME_BASELINE')
    require(idle.get('appId') == contract['expectedIdle'] and idle.get('support') is True,
            'IDLE_BASELINE')
    config = snapshot['configIdleApp']
    require(type(config) is dict and config.get('appId') == contract['expectedIdle'],
            'CONFIG_IDLE_BASELINE')
    profile_list = snapshot['configProfileList']
    require(type(profile_list) is list and all(type(v) is str for v in profile_list) and
            len(profile_list) == len(set(profile_list)) and 'idleApp' in profile_list,
            'CONFIG_IDLE_GATE')
    require(snapshot['lastAppHandlerPolicy'] == 'idleApp', 'IDLE_POLICY')
    prior = snapshot['factoryIdleApp']
    exact(prior, ('present', 'value'), 'FACTORY_SCHEMA')
    require(type(prior['present']) is bool and
            (type(prior['value']) is dict if prior['present'] else prior['value'] is None),
            'FACTORY_PRIOR')
    home_after, idle_after, config_after = map(copy.deepcopy, (defaults, idle, config))
    home_after['categories']['home'] = owner
    idle_after['appId'] = owner
    config_after['appId'] = owner
    # Factory rollback restores the full prior selected value, or removes the
    # selected key with null. It never restores an entire DB over unrelated data.
    return {
        'home_profile.candidate.json': {'defaultApps': home_after},
        'idle_profile.candidate.json': {'idleApp': idle_after},
        'config-set.candidate.json': {'configs': {'profile.idleApp': config_after}, 'volatile': False},
        'config-restore.candidate.json': {'configs': {'profile.idleApp': copy.deepcopy(prior['value'])}, 'volatile': False},
        'profile-restore.reference.json': {'defaultApps': copy.deepcopy(defaults), 'idleApp': copy.deepcopy(idle)},
    }

def plan(snapshot_raw, contract, owner, original_root):
    verified = verify_contract(contract, original_root)
    require(digest(snapshot_raw) == contract['snapshotSha256'], 'SNAPSHOT_HASH')
    snapshot = decode(snapshot_raw)
    outputs = {name: encode(value) for name, value in candidates(snapshot, contract, owner).items()}
    changed = [field for field, before in (
        ('defaultApps.categories.home', snapshot['profile']['defaultApps']['categories']['home']),
        ('idleApp.appId', snapshot['profile']['idleApp']['appId']),
        ('profile.idleApp.appId', snapshot['configIdleApp']['appId'])) if before != owner]
    summary = {'schema': 1, 'mode': 'offline-candidate-only', 'deploymentImplemented': False,
               'firmwareClaim': 'matches-supplied-contract-not-independently-qualified',
               'sourceSha256': digest(snapshot_raw), 'originalsVerified': verified,
               'outputs': {n: {'sha256': digest(b), 'bytes': len(b)} for n, b in outputs.items()},
               'changedFields': changed,
               'supportAndOtherFieldsPreserved': True, 'liveCalls': 0}
    outputs['plan.json'] = encode(summary)
    return outputs

def publish(output, files):
    output = Path(output).absolute()
    require(output.parent.resolve() == output.parent, 'OUTPUT_PARENT')
    # Nothing is ever replaced. An incomplete output directory is retained.
    output.mkdir(mode=0o700)
    os.chmod(output, 0o700)
    for name, data in files.items():
        require('/' not in name and '\\' not in name, 'OUTPUT_NAME')
        fd = os.open(output / name, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
        with os.fdopen(fd, 'wb') as f:
            os.fchmod(f.fileno(), 0o600); f.write(data); f.flush(); os.fsync(f.fileno())
    fd = os.open(output, os.O_RDONLY | os.O_NOFOLLOW)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)

def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--contract', required=True, type=Path)
    parser.add_argument('--snapshot', required=True, type=Path)
    parser.add_argument('--original-root', required=True, type=Path)
    parser.add_argument('--owner-app-id', required=True)
    parser.add_argument('--output', required=True, type=Path)
    args = parser.parse_args()
    _, raw_contract = file_hash(args.contract, MAX_JSON, keep=True)
    _, raw_snapshot = file_hash(args.snapshot, MAX_JSON, keep=True)
    files = plan(raw_snapshot, decode(raw_contract), args.owner_app_id, args.original_root)
    publish(args.output, files)
    print(json.dumps({'mode': 'offline-candidate-only', 'filesWritten': len(files),
                      'liveCalls': 0, 'deploymentImplemented': False}))

if __name__ == '__main__':
    try:
        main()
    except (Refusal, OSError, ValueError, TypeError, KeyError) as error:
        # Never print source values, paths, profile data, or exception text.
        code = str(error) if type(error) is Refusal else 'LOCAL_INPUT_OR_OUTPUT_REFUSED'
        print(json.dumps({'error': code, 'liveCalls': 0}), file=sys.stderr)
        raise SystemExit(1)
