"""Whitelisted process memory/security scalar readback, never map names/paths."""
from pathlib import Path
import re
if __package__:
    from .controller import Refused
else:
    from controller import Refused

FIELDS={'VmLck':'locked_bytes','VmRSS':'rss_bytes','VmSwap':'swap_bytes','VmSize':'virtual_bytes',
        'Threads':'threads','NoNewPrivs':'no_new_privs','Seccomp':'seccomp_mode'}


def parse_status(raw):
    if type(raw)is not str or len(raw)>65536:raise Refused('MEMORY_STATUS')
    row={'schema':1}
    for line in raw.splitlines():
        key,separator,value=line.partition(':')
        if key not in FIELDS:continue
        if key in ('VmLck','VmRSS','VmSwap','VmSize'):
            match=re.fullmatch(r'\s*([0-9]+)\s+kB\s*',value)
            if not match:raise Refused('MEMORY_STATUS')
            number=int(match[1])*1024
        else:
            match=re.fullmatch(r'\s*([0-9]+)\s*',value)
            if not match:raise Refused('MEMORY_STATUS')
            number=int(match[1])
        if not 0<=number<=2**63-1 or FIELDS[key]in row:raise Refused('MEMORY_STATUS')
        row[FIELDS[key]]=number
    if set(row)!={'schema',*FIELDS.values()}:raise Refused('MEMORY_STATUS')
    return row


def summary():
    with open('/proc/self/status',encoding='ascii')as stream:
        return parse_status(stream.read(65537))


def locked_and_filtered(row):
    return (type(row)is dict and row.get('locked_bytes',0)>0 and row.get('swap_bytes')==0 and
            row.get('no_new_privs')==1 and row.get('seccomp_mode')==2)
