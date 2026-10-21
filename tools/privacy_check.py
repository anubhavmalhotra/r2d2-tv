#!/usr/bin/env python3
"""Inspect the Git index, or with --history every reachable blob and commit
message, without printing suspicious contents. No network."""
import base64
from fnmatch import fnmatchcase
import hashlib
import json
from pathlib import Path
import re
import struct
import subprocess
import sys

ROOT=Path(__file__).resolve().parents[1]
FORBIDDEN_PARTS={'private','backups','receipts','logs','dist','.build','.env','.venv','__pycache__',
                 'experimental'}
FORBIDDEN_SUFFIXES={'.ipk','.epk','.img','.bin','.squashfs','.pem','.key','.log','.pcap','.pcapng','.pyc',
                    '.crt','.cer','.der','.p12','.pfx','.jks','.keystore'}
# Lower-cased mirror of the .gitignore key, pairing and credential name rules.
FORBIDDEN_NAMES=('id_rsa*','id_ed25519*','*ssap*key*.json','*ssap*pin*.json','*client-key*','*pairing*.json',
                 '*identity*.json','credentials*.json','secrets.*','.npmrc','.netrc','.env*')
PATTERNS={
    'private-key':re.compile(rb'-----BEGIN [A-Z0-9 ]*PRIVATE KEY(?: BLOCK)?-----|PuTTY-User-Key-File-[0-9]+:'),
    'github-token':re.compile(rb'\bgh[pousr]_[A-Za-z0-9]{30,}\b|\bgithub_pat_[A-Za-z0-9_]{30,}'),
    'provider-token':re.compile(rb'\bsk-(?:proj-|ant-)?[A-Za-z0-9_-]{30,}'),
    'personal-home-path':re.compile(rb'(?:(?<=file://)|(?<=//localhost)|(?<![A-Za-z0-9_./-]))'
                                    rb'(?:/Users/[A-Za-z0-9_.-]+|/home/(?!\{\{)[A-Za-z0-9_.-]+)'
                                    rb'|(?<![A-Za-z0-9_./-])~/(?:Documents|Desktop|Downloads|Library)/'),
    'hardware-address':re.compile(rb'(?<![A-Fa-f0-9:])(?:[A-Fa-f0-9]{2}:){5}[A-Fa-f0-9]{2}(?![A-Fa-f0-9:])'),
    'private-ipv4':re.compile(rb'\b(?:10\.[0-9]{1,3}|192\.168|172\.(?:1[6-9]|2[0-9]|3[01]))\.[0-9]{1,3}\.[0-9]{1,3}\b'),
    'cgnat-ipv4':re.compile(rb'(?<![0-9.])100\.(?:6[4-9]|[7-9][0-9]|1[01][0-9]|12[0-7])\.[0-9]{1,3}\.[0-9]{1,3}(?!\.?[0-9])'),
    'private-ipv6':re.compile(rb'(?i)(?<![0-9a-z:.])(?:f[cd][0-9a-f]{2}|fe[89ab][0-9a-f])(?::{1,2}[0-9a-f]{1,4}){1,7}(?:::)?(?![0-9a-z:])'),
    'embedded-client-key':re.compile(rb'["\'](?:client-key|pairingKey|browserKey|password)["\']\s*:\s*["\'][^"\']{8,}["\']'),
    'embedded-capability':re.compile(rb'["\'](?:key|token|secret|access_token)["\']\s*:\s*["\'][a-fA-F0-9]{48,128}["\']'),
    'hex-credential':re.compile(rb'(?<![A-Za-z0-9_-])([A-Za-z_][A-Za-z0-9_-]*)["\']?\s*[:=]\s*(?:Bearer\s+)?["\']?'
                                rb'([A-Fa-f0-9]{32,128})(?![A-Za-z0-9])'),
    'personal-email':re.compile(rb'(?<![A-Za-z0-9._%+/-])([A-Za-z0-9._%+-]+)@'
                                rb'([A-Za-z0-9-]+(?:\.[A-Za-z0-9-]+)*\.[A-Za-z]{2,24})(?![A-Za-z0-9-])'),
    'postal-code':re.compile(rb'(?i)(?<!g)(?:zip|postal|postcode)(?:[ _-]?code)?["\']?\s*[:=]?\s*["\']?'
                             rb'([0-9]{5})(?:-[0-9]{4})?(?![0-9])'),
    'location-coordinates':re.compile(rb'(?<![0-9A-Za-z_.])(-?[0-9]{1,3}\.[0-9]{3,})\s*,\s*(-?[0-9]{1,3}\.[0-9]{3,})(?![0-9.])'
                                      rb'|(?i:(?<![a-z])lat(?:itude)?)["\']?\s*[:=]\s*["\']?(-?[0-9]{1,3}\.[0-9]{3,})["\']?\s*,\s*'
                                      rb'["\']?(?i:lo?ng(?:itude)?|lon)["\']?\s*[:=]\s*["\']?(-?[0-9]{1,3}\.[0-9]{3,})'),
    'weather-station-id':re.compile(rb'(?<![A-Za-z0-9])K[A-Z]{3}(?![A-Za-z0-9])'),
}
RFC1918_CIDRS=(b'10.0.0.0/8',b'172.16.0.0/12',b'192.168.0.0/16')
# Range edges of 100.64.0.0/10, used by address-class tests and definitions.
CGNAT_EDGES={b'100.64.0.0',b'100.64.0.1',b'100.127.255.255'}
CREDENTIAL_NAME=re.compile(rb'(?i)key|token|secret|pass|cred|auth')
DIGEST_NAME=re.compile(rb'(?i)sha|pin|hash|digest|revision|mac')
PLACEHOLDER_DOMAINS=(b'example.com',b'example.org',b'example.net',b'example.invalid',b'users.noreply.github.com')
RESERVED_TLDS={b'example',b'test',b'invalid',b'localhost'}
# Public service addresses, not people.
PLACEHOLDER_EMAILS={b'git@github.com',b'git@gitlab.com'}
STATION_CONTEXT=re.compile(rb'(?i)station|metar|icao')

# These exact reviewed public filter files contain two generic router-address
# exceptions. They are filter definitions, not this installation's addresses.
# Only these exact paths AND bytes may exceed 2 MiB or contain those definitions.
PUBLIC_FILTER_DATA={
    'protection/upstream/easyprivacy.json':'3a1f9408dc91f17db7d4ddeec176a7f7457616c67780f4855af076801dc495c0',
    'spark/runtime/extension/rules/easyprivacy.json.in':'4b3a3b017ae9b4b9997b96d93cf62df2fd582db223e7b617819e173698ab3895',
}

# Two occurrences in this exact public dependency manifest are the NVIDIA
# curand version, once as a version and once in its official wheel filename.
# Keep the exact path, whole-file hash, literal and count checks: any changed
# manifest or additional address falls back to the ordinary private-IP check.
PUBLIC_VERSION_METADATA={
    'voice/backend/dependencies.json':
        ('41e6a805c2e067fa3153f506e6b2477848e5b4c322845f86badc97be44a0beb9',
         '13afd9b75d7bc7f72a6a2361dec9e6d1b7df880294ead82b21eb5ad4eba5968b', 2),
}


def reviewed_version_literals(name, raw):
    pin=PUBLIC_VERSION_METADATA.get(name)
    if pin is None or hashlib.sha256(raw).hexdigest()!=pin[0]:
        return False
    matches=list(PATTERNS['private-ipv4'].finditer(raw))
    return len(matches)==pin[2] and all(hashlib.sha256(match.group()).hexdigest()==pin[1]
                                      for match in matches)


def published_cidr(match, raw):
    # Published address-space definitions are not a configured device IP.
    return any(raw[match.start():].startswith(cidr) and
               (len(raw)==match.start()+len(cidr) or raw[match.start()+len(cidr)] not in b'0123456789.')
               for cidr in RFC1918_CIDRS)


def digest_value(match, raw):
    # The last naming word decides: keyHash is a digest, hmac_key is a key.
    # Values with four or fewer distinct digits are visibly synthetic fixtures.
    name,value=match[1],match[2]
    credential=[m.end() for m in CREDENTIAL_NAME.finditer(name)]
    if not credential or len(set(value.lower()))<=4:return True
    digest=[m.end() for m in DIGEST_NAME.finditer(name)]
    return bool(digest) and max(digest)>max(credential)


def placeholder_email(match, raw):
    local,domain=match[1].lower(),match[2].lower()
    return (local in (b'noreply',b'no-reply') or domain.rsplit(b'.',1)[-1] in RESERVED_TLDS or
            any(domain==d or domain.endswith(b'.'+d) for d in PLACEHOLDER_DOMAINS) or
            match[0].lower() in PLACEHOLDER_EMAILS or
            re.match(rb'[0-9]+\.[0-9]+',domain) is not None)  # package@1.2.3 specifiers


def synthetic_coordinates(match, raw):
    lat,lon=float(match[1] or match[3]),float(match[2] or match[4])
    if abs(lat)>90 or abs(lon)>180:return True
    if match[1]:
        # A pair inside a longer numeric list or after a lone SVG path command is drawing data.
        before=raw[max(0,match.start()-8):match.start()].rstrip()
        after=raw[match.end():match.end()+8].lstrip()[:1]
        command=before[-1:].isalpha() and before[-1:] in b'MLCSQTAmlcsqta' and not before[-2:-1].isalpha()
        if command or before[-1:] and before[-1:] in b',0123456789' or after and after in b',0123456789':return True
    # Test fixtures sit at the 40N 100W grid point, near null island, or are the 40.1234,-100.5678 sequence.
    return ((abs(lat-40)<0.01 and abs(lon+100)<0.01) or (abs(lat)<0.01 and abs(lon)<0.01) or
            (lat,lon)==(40.1234,-100.5678))


def synthetic_station(match, raw):
    # KAAA, KBBB ... KZZZ are the test fixture set; elsewhere only station contexts matter.
    word=match.group()
    if word[1:2]==word[2:3]==word[3:4]:return True
    return not STATION_CONTEXT.search(raw[max(0,match.start()-160):match.end()+160])


ALLOWED={
    'private-ipv4':published_cidr,
    'cgnat-ipv4':lambda match,raw:match.group() in CGNAT_EDGES,
    'hex-credential':digest_value,
    'personal-email':placeholder_email,
    'postal-code':lambda match,raw:match[1]==b'00000',
    'location-coordinates':synthetic_coordinates,
    'weather-station-id':synthetic_station,
}


def contains(pattern, raw, code):
    allowed=ALLOWED.get(code)
    return any(not (allowed and allowed(match,raw)) for match in pattern.finditer(raw))


def content_issues(raw, skip=()):
    return [code for code,pattern in PATTERNS.items() if code not in skip and contains(pattern,raw,code)]


def issues(name, raw):
    path=Path(name)
    found=[]
    lower=path.name.lower()
    if (any(p in FORBIDDEN_PARTS for p in path.parts) or path.suffix.lower() in FORBIDDEN_SUFFIXES or
            '.local.' in path.name or '.private.' in path.name or
            any(fnmatchcase(lower,rule) for rule in FORBIDDEN_NAMES)):
        found.append('private-or-generated-path')
    public_filter=PUBLIC_FILTER_DATA.get(name)==hashlib.sha256(raw).hexdigest()
    if len(raw)>2*1024*1024 and not public_filter:
        found.append('unexpected-large-file')
    skip=('private-ipv4',) if public_filter or reviewed_version_literals(name,raw) else ()
    found.extend(content_issues(raw,skip))
    # Images are supplied static art, never screen captures. Inspect embedded
    # textual metadata and decoded data URIs as well as the visible source.
    for match in re.finditer(rb'data:image/(?:png|svg\+xml);base64,([A-Za-z0-9+/=]+)',raw):
        try:decoded=base64.b64decode(match[1],validate=True)
        except ValueError:
            found.append('invalid-embedded-image');continue
        found.extend('embedded-'+code for code in content_issues(decoded))
        if decoded.startswith(b'\x89PNG\r\n\x1a\n'):
            found.extend(png_issues(decoded))
    if raw.startswith(b'\x89PNG\r\n\x1a\n'):found.extend(png_issues(raw))
    return sorted(set(found))


def png_issues(raw):
    offset=8
    while offset+12<=len(raw):
        size=struct.unpack('>I',raw[offset:offset+4])[0]
        kind=raw[offset+4:offset+8]
        if size>len(raw)-offset-12:return ['invalid-png']
        if kind in (b'eXIf',b'tEXt',b'zTXt',b'iTXt'):return ['image-metadata-needs-review']
        offset+=12+size
        if kind==b'IEND':return [] if offset==len(raw) else ['png-trailing-data']
    return ['invalid-png']


def git(*args):
    return subprocess.run(['git',*args],cwd=ROOT,check=True,stdout=subprocess.PIPE).stdout


def index_files():
    for entry in git('ls-files','--stage','-z').split(b'\0'):
        if not entry:continue
        meta,name=entry.split(b'\t',1)
        mode,oid,stage=meta.split()
        if mode not in (b'100644',b'100755') or stage!=b'0':
            raise ValueError('INDEX_MODE_OR_CONFLICT')
        yield name.decode('utf-8'),git('cat-file','blob',oid.decode())


def history_blobs():
    # rev-list names each reachable object once, at the first path it was seen.
    names={}
    for line in git('rev-list','--all','--objects').splitlines():
        oid,_,name=line.partition(b' ')
        if name:names.setdefault(oid,name.decode('utf-8','replace'))
    with subprocess.Popen(['git','cat-file','--batch'],cwd=ROOT,stdin=subprocess.PIPE,stdout=subprocess.PIPE) as batch:
        for oid,name in names.items():
            batch.stdin.write(oid+b'\n');batch.stdin.flush()
            header=batch.stdout.readline().split()
            if len(header)!=3:raise ValueError('UNREADABLE_OBJECT')
            raw=batch.stdout.read(int(header[2])+1)[:-1]
            if header[1]==b'blob':yield oid.decode(),name,raw
        batch.stdin.close()


def history():
    findings=[]
    blobs=0
    for oid,name,raw in history_blobs():
        blobs+=1
        if codes:=issues(name,raw):findings.append({'blob':oid,'path':name,'codes':codes})
    messages=[entry for entry in git('log','--all','-z','--format=%H%n%B').split(b'\0') if entry.strip()]
    for entry in messages:
        commit,_,message=entry.strip(b'\n').partition(b'\n')
        if codes:=sorted(content_issues(message)):findings.append({'commit':commit.decode(),'codes':codes})
    return {'passed':not findings,'blobs':blobs,'commits':len(messages),'findings':findings}


def main(argv=sys.argv[1:]):
    if argv not in ([],['--history']):
        print(json.dumps({'passed':False,'reason':'Usage: privacy_check.py [--history]'}));return 2
    if argv:
        try:report=history()
        except (OSError,ValueError,subprocess.CalledProcessError):
            print(json.dumps({'passed':False,'reason':'Readable Git history required'}));return 1
        print(json.dumps(report,indent=2));return int(not report['passed'])
    try:files=list(index_files())
    except (OSError,ValueError,subprocess.CalledProcessError):
        print(json.dumps({'passed':False,'reason':'Readable staged source index required'}));return 1
    if not files:
        print(json.dumps({'passed':False,'reason':'Empty index'}));return 1
    findings=[{'path':name,'codes':codes} for name,raw in files if (codes:=issues(name,raw))]
    print(json.dumps({'passed':not findings,'files':len(files),'findings':findings},indent=2))
    return bool(findings)


if __name__=='__main__':raise SystemExit(main())
