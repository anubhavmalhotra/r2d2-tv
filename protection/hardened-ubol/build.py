#!/usr/bin/env python3
"""Reproduce the audited extension from pinned public inputs; no downloads/install."""
import argparse
import base64
import hashlib
import io
import json
from pathlib import Path, PurePosixPath
import shutil
import stat
import subprocess
import sys
import tarfile
import tempfile
import zipfile

HERE = Path(__file__).resolve().parent
ZIP_SHA = '73909c8f26d608b00a99f446192630868bea4f947ba293c29bbd18ae948d903b'
ACORN_SRI = 'NZyJarBfL7nWwIq+FDL6Zp/yHEhePMNnnJ0y3qfieCrmNvYct8uvtiV41UvlSe6apAfk0fY1FbWx+NwfmpvtTg=='


def need(condition, code):
    if not condition:
        raise ValueError(code)


def relative_file(name):
    path = PurePosixPath(name)
    need(name == path.as_posix() and not path.is_absolute()
         and '..' not in path.parts and '\\' not in name, 'ARCHIVE_PATH')
    return path


def read_bounded(path, limit):
    with path.open('rb') as stream:
        data = stream.read(limit + 1)
    need(len(data) <= limit, 'INPUT_SIZE')
    return data


def extract_zip(source, target):
    with zipfile.ZipFile(io.BytesIO(source)) as archive:
        items = archive.infolist()
        files = [item for item in items if not item.is_dir()]
        need(len(files) == 930 and len(items) < 1500, 'ZIP_MEMBERS')
        need(sum(item.file_size for item in files) < 64 * 1024 * 1024, 'ZIP_SIZE')
        seen = set()
        for item in items:
            if item.is_dir():
                relative_file(item.filename.rstrip('/'))
                continue
            path = relative_file(item.filename)
            need(path not in seen, 'ZIP_DUPLICATE')
            need(not stat.S_ISLNK(item.external_attr >> 16), 'ZIP_LINK')
            seen.add(path)
            output = target.joinpath(*path.parts)
            output.parent.mkdir(parents=True, exist_ok=True)
            output.write_bytes(archive.read(item))


def extract_parser(source, target):
    with tarfile.open(fileobj=io.BytesIO(source), mode='r:gz') as archive:
        items = archive.getmembers()
        need(len(items) <= 20 and sum(item.size for item in items) < 2 * 1024 * 1024,
             'PARSER_SIZE')
        seen = set()
        for item in items:
            path = relative_file(item.name)
            need(path.parts[0] == 'package' and path not in seen, 'PARSER_PATH')
            seen.add(path)
            need(item.isfile() or item.isdir(), 'PARSER_TYPE')
            if item.isdir():
                continue
            output = target.joinpath(*path.parts)
            output.parent.mkdir(parents=True, exist_ok=True)
            member = archive.extractfile(item)
            need(member is not None, 'PARSER_MEMBER')
            with member:
                output.write_bytes(member.read())


def build(upstream_zip, acorn_tar, output):
    need(not output.exists() and not output.is_symlink(), 'OUTPUT_EXISTS')
    need(shutil.which('node') is not None, 'NODE_REQUIRED')
    # Hash and consume the same immutable snapshots. Never reopen an input after
    # checking its digest: the parser is executed during the following build.
    zip_bytes = read_bounded(upstream_zip, 16 * 1024 * 1024)
    parser_bytes = read_bounded(acorn_tar, 512 * 1024)
    need(hashlib.sha256(zip_bytes).hexdigest() == ZIP_SHA, 'ZIP_DIGEST')
    parser_digest = base64.b64encode(hashlib.sha512(parser_bytes).digest()).decode()
    need(parser_digest == ACORN_SRI, 'PARSER_DIGEST')
    with tempfile.TemporaryDirectory(prefix='r2d2-ubol-build-') as directory:
        stage = Path(directory)
        extract_zip(zip_bytes, stage / 'upstream')
        extract_parser(parser_bytes, stage / 'audit-parser')
        (stage / 'upstream.zip').write_bytes(zip_bytes)
        for name in ('patch.py', 'strip-console.mjs', 'test_hardening.mjs', 'upstream-tree.json'):
            shutil.copyfile(HERE / name, stage / name)
        subprocess.run([sys.executable, '-B', str(stage / 'patch.py'), 'hardened-v2'],
                       check=True, stdout=subprocess.PIPE)
        actual = json.loads((stage / 'hardened-v2-pins.json').read_text())
        need(actual == json.loads((HERE / 'expected-files.json').read_text()), 'OUTPUT_PINS')
        result = subprocess.run([shutil.which('node'), '--no-warnings',
                                 '--experimental-vm-modules', str(stage / 'test_hardening.mjs'),
                                 'hardened-v2'], check=True, stdout=subprocess.PIPE, text=True)
        checks = json.loads(result.stdout)
        need(checks.get('checksPassed') == 8, 'HARDENING_CHECKS')
        output.mkdir(parents=True, exist_ok=False)
        shutil.copytree(stage / 'hardened-v2', output / 'extension')
        shutil.copyfile(stage / 'hardened-v2-pins.json', output / 'files.json')
        receipt = json.loads((stage / 'hardened-v2-receipt.json').read_text())
        receipt['checks'] = checks
        (output / 'build-result.json').write_text(json.dumps(receipt, indent=2) + '\n')
    return {'built': True, 'files': len(actual), 'checksPassed': 8,
            'installed': False, 'nativeBrowserVerified': False}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--upstream-zip', type=Path, required=True)
    parser.add_argument('--acorn-tar', type=Path, required=True)
    parser.add_argument('--output', type=Path, required=True)
    args = parser.parse_args()
    print(json.dumps(build(args.upstream_zip, args.acorn_tar, args.output)))


if __name__ == '__main__':
    main()
