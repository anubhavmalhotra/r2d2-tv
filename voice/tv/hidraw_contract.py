"""Evidenced LS2 request/reply contract only. No native client or socket opener.

Parsed device data and a socketPath are never capture/permission/ownership proof.
The type0 socket's reservation FIFO needs its separate actual binding.
"""
import json,math,re,time
SERVICE='com.webos.service.voiceinput.hidraw'
MAX_REPLY=8192
class HidrawRefused(ValueError):pass
def need(ok,code):
    if not ok:raise HidrawRefused(code)
def byte_length(value):
    try:return len(value.encode('utf-8'))
    except UnicodeError:raise HidrawRefused('REPLY_UNQUALIFIED')from None
def parse(raw):
    need(type(raw)is bytes and 1<=len(raw)<=MAX_REPLY,'REPLY_BOUND')
    def pairs(items):
        out={}
        for k,v in items:
            need(k not in out,'REPLY_DUPLICATE');out[k]=v
        return out
    try:return json.loads(raw.decode('utf-8'),object_pairs_hook=pairs,parse_constant=lambda _:(_ for _ in()).throw(HidrawRefused('REPLY_NONFINITE')))
    except HidrawRefused:raise
    except(ValueError,UnicodeError,RecursionError):raise HidrawRefused('REPLY_UNQUALIFIED')from None
class _RemoteRecord:
    def __init__(self,identifier,key,clock):
        self.identifier=identifier;self.key=bytearray(key.encode('ascii'));self.clock=clock;self.created=clock();self.consumed=False
    def __repr__(self):return '<RemoteRecord private>'
    def retire(self):self.key[:]=b'\0'*len(self.key);self.key.clear();self.consumed=True
    def take(self):
        now=self.clock()
        try:need(not self.consumed and type(now)in(int,float)and math.isfinite(now)and type(self.created)in(int,float)and math.isfinite(self.created)and 0<=now-self.created<3,'DEVICE_STALE')
        except BaseException:self.retire();raise
        self.consumed=True;key=self.key;self.key=bytearray();return self.identifier,key,now
def inventory_request():return SERVICE+'/getDevices',{}
def parse_inventory(raw,*,clock=time.monotonic):
    value=parse(raw);records=[]
    try:
        need(type(value)is dict and set(value)in({'returnValue','devices'},{'returnValue','devices','subscribed'})and value['returnValue']is True,'INVENTORY_UNCONFIRMED')
        if 'subscribed'in value:need(type(value['subscribed'])is bool,'REPLY_UNQUALIFIED')
        need(type(value['devices'])is list and len(value['devices'])<=32,'INVENTORY_BOUND');seen=set()
        for device in value['devices']:
            need(type(device)is dict and set(device)=={'id','key','deviceType','deviceName'},'DEVICE_SCHEMA')
            need(all(type(device[k])is str for k in device),'DEVICE_SCHEMA')
            need(1<=len(device['id'])<=64 and device['id']not in seen,'DEVICE_SCHEMA');seen.add(device['id'])
            need(device['deviceType']in('remote','sound','mobile','internal')and byte_length(device['deviceName'])<=256 and len(device['key'])<=128 and all(32<=ord(c)<=126 for c in device['key']),'DEVICE_SCHEMA')
            if device['deviceType']=='remote':
                need(re.fullmatch('remote[0-9]{1,8}',device['id'])is not None,'REMOTE_ID_UNQUALIFIED')
                records.append(_RemoteRecord(device['id'],device['key'],clock))
        return records,{'devicesObserved':len(value['devices']),'remoteRecords':len(records),'nativeCallerOrCaptureQualified':False}
    except BaseException:
        for record in records:record.retire()
        raise
class HidrawCaptureContract:
    def __init__(self,record):
        need(type(record)is _RemoteRecord,'DEVICE_RECORD_UNQUALIFIED');self.clock=record.clock;self.inventoryCreated=record.created;self.identifier,self._key,self.created=record.take();self.last=self.created;self.deadline=self.created+10;self.phaseDeadline=None;self.state='new';self.path=None;self.startAck=False;self.stopAck=False
    def __repr__(self):return '<HidrawCaptureContract private>'
    def _now(self):
        now=self.clock();need(type(now)in(int,float)and math.isfinite(now)and now>=self.last,'SESSION_CLOCK');self.last=now;return now
    def start_request(self):
        need(self.state=='new','START_REPLAY')
        try:now=self._now();need(now-self.inventoryCreated<3 and now<self.deadline,'START_EXPIRED')
        except BaseException:self.close();raise
        self.state='start-pending';self.phaseDeadline=min(self.deadline,now+2)
        return SERVICE+'/startStreaming',{'id':self.identifier,'key':self._key.decode('ascii'),'socketType':'socket','subscribe':True}
    def observe_start(self,raw):
        need(self.state=='start-pending','START_REPLAY')
        try:
            need(self._now()<self.phaseDeadline,'START_EXPIRED')
            v=parse(raw);allowed={'returnValue','id','state','socketPath'}
            need(type(v)is dict and set(v)in(allowed,allowed|{'subscribed'})and v['returnValue']is True and v['id']==self.identifier and v['state']=='voice start','START_UNCONFIRMED')
            if 'subscribed'in v:need(type(v['subscribed'])is bool,'START_UNCONFIRMED')
            path=v['socketPath'];need(type(path)is str and byte_length(path)<=107 and re.fullmatch('/var/voiceinput/[A-Za-z0-9_-][A-Za-z0-9_.-]{0,79}\\.soc',path)is not None,'SOCKET_PATH_UNQUALIFIED')
            need(self._now()<self.phaseDeadline,'START_EXPIRED')
            self.path=path;self.startAck=True;self.state='start-reply-observed'
        except BaseException:self.state='start-unknown';raise
    def start_unknown(self):
        need(self.state=='start-pending','START_REPLAY');self.state='start-unknown'
    def stop_request(self):
        need(self.state in('start-pending','start-reply-observed','start-unknown'),'STOP_REPLAY')
        try:now=self._now()
        except BaseException:
            self.state='stop-pending';self.stop_unknown();raise
        self.state='stop-pending';self.phaseDeadline=now+2
        return SERVICE+'/stopStreaming',{'id':self.identifier,'key':self._key.decode('ascii')}
    def observe_stop(self,raw):
        need(self.state=='stop-pending','STOP_REPLAY')
        try:
            need(self._now()<self.phaseDeadline,'STOP_EXPIRED')
            v=parse(raw)
            need(type(v)is dict and set(v)in({'returnValue'},{'returnValue','id','state','reason'})and v['returnValue']is True,'STOP_UNCONFIRMED')
            if 'id'in v:need(v['id']==self.identifier and v['state']=='voice stop'and v['reason']=='audio device stopped','STOP_UNCONFIRMED')
            need(self._now()<self.phaseDeadline,'STOP_EXPIRED')
            self.stopAck=True;self.state='stop-reply-observed'
        except BaseException:self.state='stop-unknown';raise
        finally:self._key[:]=b'\0'*len(self._key);self._key.clear();self.path=None
    def stop_unknown(self):
        need(self.state=='stop-pending','STOP_REPLAY');self.state='stop-unknown';self._key[:]=b'\0'*len(self._key);self._key.clear();self.path=None
    def close(self):
        self._key[:]=b'\0'*len(self._key);self._key.clear();self.path=None;self.state='retired'
    def tick(self):
        # The external owner must call this and supervise the actual RPC/provider.
        # This library returns at most one stop decision; it performs no I/O.
        now=self.clock()
        invalid=type(now)not in(int,float)or not math.isfinite(now)or now<self.last
        expired=invalid or now>=self.deadline
        if not invalid:self.last=now
        if self.state=='stop-pending':
            # Cleanup acknowledgement has its own two-second budget after capture expiry.
            if invalid or now>=self.phaseDeadline:self.stop_unknown()
            return None
        if expired:
            if self.state=='new':self.close();return None
            if self.state in('start-pending','start-reply-observed','start-unknown'):
                if invalid:
                    self.state='stop-pending';self.stop_unknown();return None
                return self.stop_request()
        if self.state=='start-pending'and now>=self.phaseDeadline:self.start_unknown()
        return None
    def read_parameters(self):
        # A successful LS2 reply cannot prove the FIFO client/socket/format binding.
        raise HidrawRefused('CAPTURE_SOCKET_BINDING_UNQUALIFIED')
    def summary(self):return {'state':self.state,'startReplyObserved':self.startAck,'stopReplyObserved':self.stopAck,'nativeCallerQualified':False,'reservationSocketBound':False,'audioFormatBound':False,'physicalMicStoppedProven':False,'microphoneCaptured':False}
if __name__=='__main__':print('{"preparedOnly":true,"nativeCalls":0,"socketOpened":false,"microphoneActivated":false}')
