"""GPL-3.0-or-later. Fixed JSON literals from public filter source, never JS evaluation.

Generic CSS conservatively excludes every host with an upstream exception.
Owner endpoint denial is authored browser policy, not system-wide TV isolation.
"""
import hashlib,json,re
from pathlib import Path
ROOT=Path(__file__).resolve().parent
COMMIT='0d79e9ea78e8bfe31fa139abbce5d63957638fa3'
PINS={'scripting__specific__easylist.json':'7192c61de0dbcf0e92678d3b2f2c924bb94f93f1ba9e2fb130b496c7a3196107',
'scripting__generic__easylist.js':'b84a5099e37db537de46b685d0e7aa56f6f19eae7e3002e74baf60ba718bccc1',
'generic-details.json':'06fb56119ba1ce8405da5db41489d8a4acc34b98f6015fc54401bf58588b878d'}
# Public LG endpoint seeds for an explicit browser-owner policy. This is not an
# exhaustive list or a TV egress firewall.
RESOURCE_TYPES='main_frame sub_frame stylesheet script image font object xmlhttprequest ping csp_report media websocket webtransport webbundle other'.split()
HOSTS=('snu.lge.com','snu-dev.lge.com','redirect.lgsmartplatform.com','qt-us.ad.lgsmartad.com','156.147.69.32')
def need(ok,code):
 if not ok:raise ValueError(code)
def dump(x):return (json.dumps(x,sort_keys=True,separators=(',',':'))+'\n').encode()
def digest(x):return hashlib.sha256(x).hexdigest()
def read_data(name):
 need(name in PINS,'SOURCE_NAME')
 p=ROOT/'upstream'/name;need(not p.is_symlink(),'SOURCE_LINK');b=p.read_bytes();need(len(b)<=2097152 and digest(b)==PINS[name],'SOURCE_PIN');return b.decode()
def json_literal(source,name,mapped=False):
 # Only a fixed named declaration is parsed as JSON data. No eval/import/JS AST
 # execution. Its immediately following delimiter must match the known wrapper.
 pattern=r'^const '+re.escape(name)+r' = '+(r'new Map\(' if mapped else '')+r'(?:/\* [0-9]+ \*/)?'
 found=list(re.finditer(pattern,source,re.M));need(len(found)==1,'DECLARATION')
 start=found[0].end()
 while source[start].isspace():start+=1
 value,end=json.JSONDecoder().raw_decode(source,start)
 need(source[end:end+(2 if mapped else 1)]==(');' if mapped else ';'),'LITERAL_BOUNDARY')
 return value
def selector(value):
 need(type(value)is str and 0<len(value)<=65536 and value.isascii(),'SELECTOR')
 need(all(ord(c)>=32 or c=='\n' for c in value),'SELECTOR_CONTROL')
 quote=None;escaped=False;outside=[];stack=[]
 for c in value:
  if escaped:escaped=False;outside.append(' ');continue
  if c=='\\':escaped=True;outside.append(' ');continue
  if quote:
   need(c!='\n','SELECTOR_QUOTED_NEWLINE')
   if c==quote:quote=None
   outside.append(' ');continue
  if c in "\"'":quote=c;outside.append(' ');continue
  need(c not in '{};@','SELECTOR_ESCAPE')
  if c in '[(':stack.append(c)
  elif c in '])':
   need(bool(stack) and stack.pop()=={']':'[',')':'('}[c],'SELECTOR_BALANCE')
  outside.append(c)
 need(quote is None and not escaped and not stack,'SELECTOR_BALANCE')
 visible=''.join(outside)
 need('/*'not in visible and '*/'not in visible and not re.search(r'url\s*\(',visible,re.I),'SELECTOR_ESCAPE')
 return value
def compile_generic(source):
 low=json_literal(source,'lowlyGeneric',True);high=json_literal(source,'highlyGeneric');exceptions=json_literal(source,'exceptions');hosts=json_literal(source,'hostnames');entities=json_literal(source,'hasEntities')
 need(type(low)is list and 0<len(low)<=20000 and type(high)is str,'GENERIC_SHAPE')
 need(entities is False and type(exceptions)is list and type(hosts)is list and len(exceptions)==len(hosts)<=2000,'EXCEPTION_SHAPE')
 selectors=[];ids=set()
 for row in low:
  need(type(row)is list and len(row)==2 and type(row[0])is int and 0<=row[0]<=65535 and row[0]not in ids,'LOW_ROW');ids.add(row[0]);selectors.append(selector(row[1]))
 selectors.append(selector(high))
 # Skip ALL generic cosmetics on exception hosts, conservatively preserving
 # their explicit upstream exception. This is broader than per-selector unhide.
 exclude=[]
 for h,s in zip(hosts,exceptions):
  selector(s);need(type(h)is str and len(h)<=253 and re.fullmatch(r'[a-z0-9]+(?:[.-][a-z0-9]+)*',h) is not None and '.'in h,'EXCEPTION_HOST')
  exclude.append('*://*.'+h+'/*')
 exclude=sorted(set(exclude))
 # Separate CSS rules mean an invalid selector does not invalidate all rules.
 css='/* GPL-3.0-or-later; EasyList data, no upstream executable code. */\n'+''.join(s+' {display:none!important;}\n' for s in selectors)
 need(len(css.encode())<=1048576,'CSS_SIZE')
 return css,exclude,len(selectors),len(exceptions)

def owner_rules():
 return [{'id':i+1,'priority':1000,'action':{'type':'block'},'condition':{'requestDomains':[h],'resourceTypes':RESOURCE_TYPES}}for i,h in enumerate(HOSTS)]
