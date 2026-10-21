#!/usr/bin/env python3
"""Build only. Never deploy, execute target binaries, or source SDK shell files."""
import argparse
import hashlib
import json
from pathlib import Path
import re
import struct
import subprocess

HERE = Path(__file__).resolve().parent
PROVIDER_SHA256 = '2b9f9e318fba385801e160349159cb3238d1fc370136a385413168170879e28b'
SOURCES = ('owned_provider.cpp', 'native_handoff.cpp', 'native_handoff.hpp', 'ui_authority.hpp',
           'ble_callback.hpp', 'native_retirement.hpp', 'provider_supervisor.c', 'supervised_policy.h', 'owner_retirement.h', 'startup_lock.hpp')


def sha(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def elf(path):
    data = path.read_bytes()
    if len(data) < 52 or data[:7] != b'\x7fELF\x01\x01\x01':
        raise ValueError('ELF32_LITTLE_ENDIAN_REQUIRED')
    header = struct.unpack_from('<16sHHIIIIIHHHHHH', data)
    if header[2] != 40 or header[1] not in (2, 3) or header[8] != 52:
        raise ValueError('ARM_EXECUTABLE_OR_DSO_REQUIRED')
    return {'elfType': header[1], 'machine': 'ARM', 'bits': 32,
            'byteOrder': 'little', 'flags': header[7], 'sha256': sha(path)}


def run(args):
    result = subprocess.run([str(x) for x in args], capture_output=True, text=True, timeout=120)
    if result.returncode:
        raise RuntimeError(result.stderr[-8000:] or 'BUILD_TOOL_FAILED')
    return result.stdout


def dependencies(readelf, path):
    dynamic = run([readelf, '-d', path])
    versions = run([readelf, '-V', path])
    needed = sorted(set(re.findall(r'\(NEEDED\).*?\[([^]]+)\]', dynamic)))
    soname = re.findall(r'\(SONAME\).*?\[([^]]+)\]', dynamic)
    requires = {}
    definitions = set()
    mode, current = '', None
    for line in versions.splitlines():
        if line.startswith('Version needs section'):
            mode, current = 'needs', None
        elif line.startswith('Version definition section'):
            mode, current = 'defines', None
        elif line.startswith('Version symbols section'):
            mode, current = '', None
        match = re.search(r'File: (\S+)', line)
        if mode == 'needs' and match:
            current = match.group(1)
            requires.setdefault(current, set())
        match = re.search(r'Name: (\S+)', line)
        if match:
            if mode == 'needs' and current:
                requires[current].add(match.group(1))
            elif mode == 'defines':
                definitions.add(match.group(1))
    return {'needed': needed, 'soname': soname[0] if len(soname) == 1 else None,
            'requires': {k: sorted(v) for k, v in sorted(requires.items())},
            'defines': sorted(definitions)}


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--sdk-root', type=Path, required=True)
    parser.add_argument('--sysroot', type=Path)
    parser.add_argument('--helper-prefix', type=Path, action='append', default=[])
    parser.add_argument('--compiler-prefix', default='arm-webos-linux-gnueabi-')
    parser.add_argument('--provider-image', type=Path, required=True,
                        help='Private exact-build original, read only; never copied into output')
    parser.add_argument('--runtime-library', action='append', default=[], metavar='SONAME=PATH',
                        help='Actual captured TV library, not SDK substitution')
    parser.add_argument('--output', type=Path, required=True)
    parser.add_argument('--build', action='store_true', help='Explicitly permit local cross-compiler execution')
    args = parser.parse_args(argv)
    image = elf(args.provider_image)
    if image['sha256'] != PROVIDER_SHA256 or image['elfType'] != 3:
        raise ValueError('EXACT_ET_DYN_PROVIDER_REQUIRED')
    sdk = args.sdk_root.resolve()
    sysroot = (args.sysroot or sdk / 'arm-webos-linux-gnueabi/sysroot').resolve()
    prefix = args.compiler_prefix
    helpers = args.helper_prefix or [sdk / 'libexec/gcc/arm-webos-linux-gnueabi/12.2.0',
        sdk / 'arm-webos-linux-gnueabi/bin', sdk / 'lib/gcc/arm-webos-linux-gnueabi/12.2.0', sdk / 'bin']
    base = [f'--sysroot={sysroot}'] + [f'-B{x.resolve()}/' for x in helpers]
    common = ['-O2', '-Wall', '-Wextra', '-Werror', '-march=armv7-a', '-mthumb',
              '-mfloat-abi=softfp', '-mfpu=neon']
    sources = {name: sha(HERE / name) for name in SOURCES}
    if not args.build:
        print(json.dumps({'buildRequested': False, 'targetExecuted': False,
            'provider': image, 'sources': sources, 'outputCreated': False}, sort_keys=True))
        return
    if args.output.exists():
        raise ValueError('OUTPUT_MUST_BE_NEW')
    cxx, cc = sdk / 'bin' / (prefix + 'g++.br_real'), sdk / 'bin' / (prefix + 'gcc.br_real')
    readelf = sdk / 'bin' / (prefix + 'readelf')
    if run([cxx, '-dumpmachine']).strip() != 'arm-webos-linux-gnueabi':
        raise ValueError('TOOLCHAIN_TARGET')
    args.output.mkdir(mode=0o700, parents=True)
    module, supervisor = args.output / 'owned-provider.so', args.output / 'provider-supervisor'
    run([cxx, *base, '-std=c++17', '-fno-optimize-sibling-calls', *common, '-fPIC', '-shared',
         f'-I{sysroot}/usr/include/glib-2.0', f'-I{sysroot}/usr/lib/glib-2.0/include',
         HERE / 'owned_provider.cpp', HERE / 'native_handoff.cpp', '-Wl,-z,relro,-z,now',
         '-ldl', '-pthread', '-o', module])
    run([cc, *base, '-std=c11', *common, HERE / 'provider_supervisor.c',
         '-Wl,-z,relro,-z,now', '-pthread', '-o', supervisor])
    artifacts = {}
    for name, path in [('module', module), ('supervisor', supervisor)]:
        metadata = elf(path)
        if metadata['flags'] != image['flags']:
            raise ValueError('ARM_ABI_FLAGS_MISMATCH')
        metadata['dynamic'] = dependencies(readelf, path)
        artifacts[name] = metadata
    runtimes = {}
    for item in args.runtime_library:
        name, separator, filename = item.partition('=')
        if not separator or not name or name in runtimes:
            raise ValueError('RUNTIME_LIBRARY_ARGUMENT')
        path = Path(filename)
        metadata = elf(path)
        if metadata['flags'] != image['flags']:
            raise ValueError('RUNTIME_ARM_ABI_MISMATCH')
        metadata['dynamic'] = dependencies(readelf, path)
        if metadata['dynamic']['soname'] != name:
            raise ValueError('RUNTIME_SONAME_MISMATCH')
        runtimes[name] = metadata
    checks, needed_versions = {}, {}
    for artifact in artifacts.values():
        for name in artifact['dynamic']['needed']:
            needed_versions.setdefault(name, set()).update(artifact['dynamic']['requires'].get(name, []))
    for name, required in needed_versions.items():
        runtime = runtimes.get(name)
        checks[name] = 'missing-captured-library' if not runtime else (
            'version-set-match' if required.issubset(runtime['dynamic']['defines']) else 'missing-required-version')
    manifest = {'schema': 1, 'buildComplete': True, 'targetExecuted': False,
        'targetLoadQualified': False, 'physicalMicrophoneQualified': False,
        'toolchainVersion': run([cxx, '-dumpfullversion']).strip(), 'provider': image,
        'sources': sources, 'artifacts': artifacts, 'capturedRuntimes': runtimes,
        'runtimeChecks': checks, 'runtimeVersionSetsMatch': bool(checks) and all(x == 'version-set-match' for x in checks.values()),
        'mode': 'capture', 'device': '', 'backendProfile': 1, 'maxMs': 10000,
        'modulePath': str(module.resolve()), 'moduleSha256': artifacts['module']['sha256'],
        'supervisorPath': str(supervisor.resolve()), 'supervisorSha256': artifacts['supervisor']['sha256']}
    (args.output / 'native-build.json').write_text(json.dumps(manifest, indent=2, sort_keys=True) + '\n')
    print(json.dumps({'buildComplete': True, 'targetExecuted': False, 'runtimeChecks': checks,
                      'manifest': str((args.output / 'native-build.json').resolve())}, sort_keys=True))


if __name__ == '__main__':
    main()
