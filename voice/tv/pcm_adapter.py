"""Fixed decoded-PCM taps to bounded LE16 ingress frames. No microphone/socket I/O.

The observed native captured-notification tap already has its BE-to-LE swap.
A caller must bind the actual selected tap/format; a string is not native proof.
"""
import math,time
TAPS={'decoder-before-native-swap':'big','capture-notify-after-native-swap':'little'}
FRAME_BYTES=640
MAX_INPUT=8192
MAX_AUDIO=320000
MAX_FRAMES=500
MAX_CALLS=4096
class PcmRefused(ValueError):pass
class Pcm16IngressAdapter:
    def __init__(self,tap,*,clock=time.monotonic):
        if type(tap)is not str or tap not in TAPS or not callable(clock):raise PcmRefused('PCM_TAP_UNBOUND')
        now=clock()
        if type(now)not in(int,float)or not math.isfinite(now):raise PcmRefused('PCM_CLOCK')
        self.order=TAPS[tap];self.clock=clock;self.started=now;self.last=now
        self.pending=bytearray();self.total=0;self.frames=0;self.calls=0;self.closed=False
    def close(self):
        self.pending[:]=b'\0'*len(self.pending);self.pending.clear();self.closed=True
    def _live(self):
        now=self.clock()
        if self.closed:raise PcmRefused('PCM_CLOSED')
        if type(now)not in(int,float)or not math.isfinite(now)or now<self.last or now-self.started>=10:raise PcmRefused('PCM_EXPIRED')
        self.last=now
    def _frame(self,size):
        if size<2 or size%2 or size>FRAME_BYTES or self.frames>=MAX_FRAMES:raise PcmRefused('PCM_FRAME_BOUND')
        out=self.pending[:size]
        if self.order=='big':
            for i in range(0,size,2):out[i],out[i+1]=out[i+1],out[i]
        self.pending[:size]=b'\0'*size;del self.pending[:size];self.frames+=1
        return out
    def feed(self,chunk):
        frames=[]
        try:
            self._live();self.calls+=1
            if type(chunk)not in(bytes,bytearray)or not 1<=len(chunk)<=MAX_INPUT or self.calls>MAX_CALLS or self.total+len(chunk)>MAX_AUDIO:raise PcmRefused('PCM_INPUT_BOUND')
            self.total+=len(chunk);self.pending.extend(chunk)
            while len(self.pending)>=FRAME_BYTES:frames.append(self._frame(FRAME_BYTES))
            self._live();return frames
        except BaseException:
            for frame in frames:frame[:]=b'\0'*len(frame)
            self.close();raise
    def finish(self):
        frames=[]
        try:
            self._live()
            if len(self.pending)%2:raise PcmRefused('PCM_PARTIAL_SAMPLE')
            if self.pending:frames.append(self._frame(len(self.pending)))
            self._live();self.close();return frames
        except BaseException:
            for frame in frames:frame[:]=b'\0'*len(frame)
            self.close();raise
if __name__=='__main__':print('{"preparedOnly":true,"microphoneActivated":false,"socketsOpened":0}')
