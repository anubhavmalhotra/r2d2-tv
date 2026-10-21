"""GPL-3.0-or-later. Offline, bounded DNR-data adapter; never evaluates upstream code."""
import base64
import copy
import hashlib
import ipaddress
import json
from pathlib import Path
import re

ROOT = Path(__file__).resolve().parent
LISTS = ('easylist', 'easyprivacy', 'ublock-badware')
RESOURCES = set('main_frame sub_frame stylesheet script image font object xmlhttprequest ping csp_report media websocket webtransport webbundle other'.split())
METHODS = set('connect delete get head options patch post put other'.split())
DOMAIN_FIELDS = {'initiatorDomains','excludedInitiatorDomains','requestDomains','excludedRequestDomains'}
CONDITIONS = DOMAIN_FIELDS | {'urlFilter','domainType','resourceTypes','excludedResourceTypes','requestMethods'}

def require(ok, reason):
    if not ok:
        raise ValueError(reason)

def dump(value):
    return (json.dumps(value, sort_keys=True, separators=(',', ':'), ensure_ascii=True)+'\n').encode()

def sha(data):
    return hashlib.sha256(data).hexdigest()

def validate_rule(rule):
    require(type(rule) is dict and set(rule) <= {'id','priority','action','condition'} and {'id','action','condition'} <= set(rule), 'RULE_SHAPE')
    require(type(rule['id']) is int and 0 < rule['id'] < 1000000, 'RULE_ID')
    require(type(rule.get('priority',1)) is int and 1 <= rule.get('priority',1) <= 100, 'PRIORITY')
    action=rule['action']; c=rule['condition']
    require(type(action) is dict and action.get('type') in ('block','allow','redirect','modifyHeaders'), 'ACTION')
    permitted={'block':{'type'},'allow':{'type'},'redirect':{'type','redirect'},'modifyHeaders':{'type','requestHeaders','responseHeaders'}}
    require(set(action)<=permitted[action['type']], 'ACTION_FIELDS')
    require(type(c) is dict and c and set(c)<=CONDITIONS, 'CONDITION_FIELDS')
    require(any(k in c for k in ('urlFilter','requestDomains','initiatorDomains')) or
            (action=={'type':'block'} and c=={'domainType':'thirdParty','resourceTypes':['ping']}), 'BROAD_RULE')
    for k,v in c.items():
        if k in DOMAIN_FIELDS:
            require(type(v) is list and 0 < len(v) <= 100000 and len(set(v)) == len(v), 'DOMAIN_LIST')
            for x in v:
                require(type(x) is str and len(x)<=253, 'DOMAIN')
                if x.startswith('[') and x.endswith(']'):
                    try: ipaddress.IPv6Address(x[1:-1])
                    except ValueError: require(False,'DOMAIN')
                else: require(re.fullmatch(r'[a-z0-9_.-]+',x) is not None,'DOMAIN')
        elif k == 'urlFilter':
            require(type(v) is str and 1 <= len(v) <= 2048 and v.isascii() and all(32<=ord(x)<=126 for x in v), 'URL_FILTER')
        elif k == 'domainType':
            require(v in ('firstParty','thirdParty'), 'DOMAIN_TYPE')
        else:
            valid=METHODS if k=='requestMethods' else RESOURCES
            require(type(v) is list and v and len(set(v))==len(v) and all(x in valid for x in v), 'CONDITION_LIST')
    require(not ('resourceTypes' in c and 'excludedResourceTypes' in c), 'RESOURCE_CONFLICT')

def convert(source):
    require(type(source) is list and 0 < len(source) <= 20000, 'RULESET_SIZE')
    ids=set(); out=[]; stats={'original':len(source),'retainedAllow':0,'retainedBlock':0,'redirectConvertedToBlock':0,'headersOmitted':0,'mainFrameOnlyOmitted':0,'broadThirdPartyPingOmitted':0}
    for original in source:
        validate_rule(original)
        require(original['id'] not in ids, 'DUPLICATE_ID'); ids.add(original['id'])
        rule=copy.deepcopy(original); kind=rule['action']['type']; c=rule['condition']
        if c=={'domainType':'thirdParty','resourceTypes':['ping']}:
            stats['broadThirdPartyPingOmitted']+=1; continue
        if kind=='allow':
            stats['retainedAllow']+=1  # Exact original condition/priority preserved.
        elif kind=='modifyHeaders':
            # Omit the unsupported action; never insert an exception that can
            # override a block in another selected list.
            stats['headersOmitted']+=1; continue
        else:
            if kind=='redirect':
                # No upstream surrogate code is shipped. Preserve the match and
                # priority but refuse the request; this can reduce compatibility.
                rule['action']={'type':'block'}
                stats['redirectConvertedToBlock']+=1
            if 'resourceTypes' in c:
                c['resourceTypes']=[x for x in c['resourceTypes'] if x!='main_frame']
                if not c['resourceTypes']:
                    stats['mainFrameOnlyOmitted']+=1; continue
            else:
                c['excludedResourceTypes']=sorted(set(c.get('excludedResourceTypes',[]))|{'main_frame'})
            stats['retainedBlock']+=1
        out.append(rule)
    stats['output']=len(out)
    return out,stats
