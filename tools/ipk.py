#!/usr/bin/env python3
"""Build an unsigned, data-only IPK using Python's standard library.
No installation, network access, firmware modification or maintainer scripts.
"""
from pathlib import Path
import gzip
import hashlib
import io
import json
import struct
import tarfile
import zlib


def png(size):
    """Our own simple four-tile app icon, drawn directly as RGBA pixels."""
    pixels = bytearray()
    for y in range(size):
        pixels.append(0)
        for x in range(size):
            background = (18, 29, 46, 255)
            tile = any(int(size*a) <= x < int(size*b) and int(size*c) <= y < int(size*d)
                       for a,b,c,d in [(0.23,0.45,0.23,0.45),(0.55,0.77,0.23,0.45),
                                       (0.23,0.45,0.55,0.77),(0.55,0.77,0.55,0.77)])
            pixels.extend((191, 212, 246, 255) if tile else background)
    def chunk(kind, data):
        return struct.pack('>I', len(data)) + kind + data + struct.pack('>I', zlib.crc32(kind+data))
    return b'\x89PNG\r\n\x1a\n' + chunk(b'IHDR', struct.pack('>IIBBBBB',size,size,8,6,0,0,0)) + chunk(b'IDAT',zlib.compress(bytes(pixels),9)) + chunk(b'IEND',b'')

def archive(files):
    output = io.BytesIO()
    with tarfile.open(fileobj=output, mode='w', format=tarfile.USTAR_FORMAT) as tar:
        directories = set()
        for name in files:
            for parent in Path(name).parents:
                if str(parent) != '.': directories.add(str(parent))
        for name in sorted(directories, key=lambda s:(s.count('/'),s)):
            entry = tarfile.TarInfo('./'+name); entry.type=tarfile.DIRTYPE;entry.mode=0o755;entry.mtime=0
            tar.addfile(entry)
        for name, content in sorted(files.items()):
            entry=tarfile.TarInfo('./'+name);entry.size=len(content);entry.mode=0o644;entry.mtime=0
            tar.addfile(entry,io.BytesIO(content))
    return gzip.compress(output.getvalue(),compresslevel=9,mtime=0)

def ar_member(name, data):
    header=f'{name + "/":<16}{0:<12}{0:<6}{0:<6}{"100644":<8}{len(data):<10}`\n'.encode('ascii')
    assert len(header)==60
    return header+data+(b'\n' if len(data)%2 else b'')
